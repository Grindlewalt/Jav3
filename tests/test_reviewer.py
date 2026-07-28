"""The isolated triage reviewer: model verdicts apply behind deterministic
guardrails, fail closed on garbage, land in the audit log, and are undoable.
All model calls are monkeypatched — the module must work without tools,
network or any main-agent context."""
import json

import pytest

from backend import db as db_mod
from backend import egress, reviewer, security
from backend.config import settings


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    egress._cut.clear()
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


def fake_model(verdicts):
    """complete_text stub returning a fixed JSON verdict array; records calls."""
    calls = []

    async def _fake(system, user, temperature=0.3):
        calls.append(user)
        return json.dumps(verdicts)

    _fake.calls = calls
    return _fake


async def seed_host(db, host, slug="proj", hits=3):
    await db.execute(
        "INSERT INTO egress_pending(project_slug, host, hit_count) VALUES (?,?,?)",
        (slug, host, hits))
    await db.commit()
    async with db.execute("SELECT id FROM egress_pending WHERE host = ?", (host,)) as cur:
        return (await cur.fetchone())["id"]


async def pending_row(db, pid):
    async with db.execute("SELECT * FROM egress_pending WHERE id = ?", (pid,)) as cur:
        return dict(await cur.fetchone())


async def alert_row(db, eid):
    async with db.execute("SELECT * FROM security_events WHERE id = ?", (eid,)) as cur:
        return dict(await cur.fetchone())


# --- verdicts apply ----------------------------------------------------------

async def test_allow_verdict_approves_and_trains_allowlist(db, monkeypatch):
    pid = await seed_host(db, "registry.npmjs.org")
    monkeypatch.setattr(reviewer, "complete_text",
                        fake_model([{"id": f"h{pid}", "verdict": "allow",
                                     "reason": "npm registry"}]))
    res = await reviewer.run()
    assert res["ok"] and res["allowed"] == 1 and res["flagged"] == 0
    assert (await pending_row(db, pid))["status"] == "approved"
    assert (await egress.decide(db, "proj", "registry.npmjs.org"))[0] == "allow"
    async with db.execute("SELECT * FROM triage_log") as cur:
        log = [dict(r) for r in await cur.fetchall()]
    assert len(log) == 1 and log[0]["action"] == "approved"
    assert json.loads(log[0]["detail"])["added_to"] == egress.GENERAL


async def test_flag_verdict_keeps_host_pending_with_reason(db, monkeypatch):
    pid = await seed_host(db, "weird-drop.xyz")
    monkeypatch.setattr(reviewer, "complete_text",
                        fake_model([{"id": f"h{pid}", "verdict": "flag",
                                     "reason": "unfamiliar file-drop"}]))
    res = await reviewer.run()
    assert res["flagged"] == 1
    row = await pending_row(db, pid)
    assert row["status"] == "pending" and row["triage_verdict"] == "flag"
    assert "file-drop" in row["triage_reason"]
    assert (await egress.decide(db, "proj", "weird-drop.xyz"))[0] == "deny"


async def test_ack_verdict_acknowledges_alert(db, monkeypatch):
    eid = await security.raise_event(db, kind="write_flag", severity="warn",
                                     project="proj", summary="write flag: new_import in x.py")
    monkeypatch.setattr(reviewer, "complete_text",
                        fake_model([{"id": f"a{eid}", "verdict": "ack",
                                     "reason": "routine dev import"}]))
    res = await reviewer.run()
    assert res["acked"] == 1
    assert (await alert_row(db, eid))["acknowledged"] == 1


# --- guardrails outrank the model -------------------------------------------

async def test_critical_alert_never_auto_acked(db, monkeypatch):
    eid = await security.raise_event(db, kind="egress_anomaly", severity="critical",
                                     summary="beacon-like cadence to c2.example")
    monkeypatch.setattr(reviewer, "complete_text",
                        fake_model([{"id": f"a{eid}", "verdict": "ack", "reason": "fine"}]))
    res = await reviewer.run()
    row = await alert_row(db, eid)
    assert row["acknowledged"] == 0 and row["triage_verdict"] == "flag"
    assert "guardrail" in row["triage_reason"]
    assert res["acked"] == 0 and res["flagged"] == 1


async def test_cut_host_never_auto_approved(db, monkeypatch):
    pid = await seed_host(db, "cutme.example")
    egress.mark_cut("proj", "cutme.example")
    monkeypatch.setattr(reviewer, "complete_text",
                        fake_model([{"id": f"h{pid}", "verdict": "allow", "reason": "sure"}]))
    await reviewer.run()
    row = await pending_row(db, pid)
    assert row["status"] == "pending" and row["triage_verdict"] == "flag"


async def test_anomaly_named_host_never_auto_approved(db, monkeypatch):
    pid = await seed_host(db, "sus.example")
    await security.raise_event(db, kind="egress_anomaly", severity="critical",
                               summary="volume spike", detail={"host": "sus.example"})
    monkeypatch.setattr(reviewer, "complete_text",
                        fake_model([{"id": f"h{pid}", "verdict": "allow", "reason": "ok"}]))
    await reviewer.run()
    assert (await pending_row(db, pid))["triage_verdict"] == "flag"


# --- fail closed -------------------------------------------------------------

async def test_garbage_model_output_touches_nothing(db, monkeypatch):
    pid = await seed_host(db, "example.com")

    async def garbage(system, user, temperature=0.3):
        return "I think you should probably allow everything!"
    monkeypatch.setattr(reviewer, "complete_text", garbage)
    res = await reviewer.run()
    assert res["ok"]
    row = await pending_row(db, pid)
    assert row["status"] == "pending" and row["triage_verdict"] is None


async def test_verdict_for_unknown_id_ignored(db, monkeypatch):
    pid = await seed_host(db, "example.com")
    monkeypatch.setattr(reviewer, "complete_text",
                        fake_model([{"id": "h99999", "verdict": "allow", "reason": "x"}]))
    await reviewer.run()
    assert (await pending_row(db, pid))["status"] == "pending"


# --- deterministic pre-pass --------------------------------------------------

async def test_already_allowlisted_host_approved_without_model(db, monkeypatch):
    pid = await seed_host(db, "pypi.org")     # seeded in the general allowlist
    fake = fake_model([])
    monkeypatch.setattr(reviewer, "complete_text", fake)
    res = await reviewer.run()
    assert res["allowed"] == 1 and fake.calls == []
    assert (await pending_row(db, pid))["status"] == "approved"


# --- undo --------------------------------------------------------------------

async def test_undo_auto_approve_removes_host_and_reflags(db, monkeypatch):
    pid = await seed_host(db, "registry.npmjs.org")
    monkeypatch.setattr(reviewer, "complete_text",
                        fake_model([{"id": f"h{pid}", "verdict": "allow", "reason": "npm"}]))
    await reviewer.run()
    async with db.execute("SELECT id FROM triage_log WHERE action='approved'") as cur:
        lid = (await cur.fetchone())["id"]
    res = await reviewer.undo(db, lid)
    assert res["ok"]
    assert (await egress.decide(db, "proj", "registry.npmjs.org"))[0] == "deny"
    row = await pending_row(db, pid)
    assert row["status"] == "pending" and row["triage_verdict"] == "flag"
    assert not (await reviewer.undo(db, lid))["ok"]      # idempotent refusal


async def test_undo_auto_ack_reopens_alert(db, monkeypatch):
    eid = await security.raise_event(db, kind="write_flag", severity="info",
                                     summary="write flag: x")
    monkeypatch.setattr(reviewer, "complete_text",
                        fake_model([{"id": f"a{eid}", "verdict": "ack", "reason": "routine"}]))
    await reviewer.run()
    async with db.execute("SELECT id FROM triage_log WHERE action='acked'") as cur:
        lid = (await cur.fetchone())["id"]
    assert (await reviewer.undo(db, lid))["ok"]
    row = await alert_row(db, eid)
    assert row["acknowledged"] == 0 and row["triage_verdict"] == "flag"


# --- status + toggle ---------------------------------------------------------

async def test_status_counts_and_toggle(db, monkeypatch):
    await seed_host(db, "one.example")
    await security.raise_event(db, kind="write_flag", severity="warn", summary="s")
    st = await reviewer.status(db)
    assert st["enabled"] is True                      # default on
    assert st["untriaged"] == {"hosts": 1, "alerts": 1}
    await reviewer.set_enabled(db, False)
    assert (await reviewer.status(db))["enabled"] is False


async def test_flagged_items_surface_in_status(db, monkeypatch):
    pid = await seed_host(db, "shady.example")
    monkeypatch.setattr(reviewer, "complete_text",
                        fake_model([{"id": f"h{pid}", "verdict": "flag", "reason": "shady"}]))
    await reviewer.run()
    st = await reviewer.status(db)
    assert [h["host"] for h in st["flagged_hosts"]] == ["shady.example"]
    assert st["untriaged"] == {"hosts": 0, "alerts": 0}


# --- triaged items are not re-examined --------------------------------------

async def test_second_run_skips_triaged_items(db, monkeypatch):
    pid = await seed_host(db, "shady.example")
    fake = fake_model([{"id": f"h{pid}", "verdict": "flag", "reason": "shady"}])
    monkeypatch.setattr(reviewer, "complete_text", fake)
    await reviewer.run()
    res = await reviewer.run()
    assert res["examined"] == 0 and len(fake.calls) == 1
