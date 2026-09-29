"""Backlog B5 (web security pages): the API halves of WEB-04, WEB-08, WEB-14,
WEB-17 and WEB-20. The page copy itself is in frontend/src/securityCopy.js and
its node test."""
import httpx
import pytest

from backend import egress, security
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app


@pytest.fixture
async def client(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


@pytest.fixture
async def db(tmp_env):
    await init_db()
    conn = await get_db()
    yield conn
    await conn.close()


# --- WEB-04: the Network header counts match the log ---------------------------

async def _event(db, host, verdict, slug="proj"):
    await egress.record_event(db, slug=slug, host=host, verdict=verdict)


async def test_summary_counts_hosts_you_approved_as_allowed(client, db):
    await _event(db, "deltamath.com", "deny")
    await egress.note_approved(db, "proj", "deltamath.com")
    await _event(db, "pypi.org", "deny")
    await egress.note_approved(db, "proj", "pypi.org", by="reviewer")
    await _event(db, "odd.example", "deny")          # still blocked
    s = (await client.get("/api/egress/summary")).json()
    # both approvals are allowed hosts now; the deny they answered no longer
    # counts as blocked, so the two numbers add up to the hosts in the log
    assert s["allowed"] == 2 and s["denied"] == 1


async def test_summary_approval_in_another_project_does_not_unblock(client, db):
    await _event(db, "deltamath.com", "deny", slug="a")
    await egress.note_approved(db, "b", "deltamath.com")
    s = (await client.get("/api/egress/summary")).json()
    assert s["denied"] == 1 and s["allowed"] == 1
    s = (await client.get("/api/egress/summary", params={"project": "a"})).json()
    assert s["denied"] == 1 and s["allowed"] == 0


# --- WEB-14: agent reports have their own list, whole text, and a way to clear them

async def _fault(db, went_wrong="write_file reported success but git status is clean",
                 conv=646):
    return await security.record_harness_fault(
        db, tried="write_file then git commit", went_wrong=went_wrong,
        expected="the commit to see the file", tool="write_file", project="proj",
        conversation_id=conv)


async def test_fault_summary_is_cut_with_an_ellipsis_and_the_board_has_the_whole_text(client, db):
    long = "the tool said done but " + "nothing changed on disk and " * 12 + "that is the fault"
    await _fault(db, long)
    ev = [e for e in await security.list_events(db) if e["kind"] == "harness_fault"][0]
    assert ev["summary"].endswith("…") and len(ev["summary"]) <= 200
    board = (await client.get(f"/api/security/events/{ev['id']}/context")).json()
    facts = {r["label"]: r["value"] for s in board["sections"] if s["type"] == "facts"
             for r in s["rows"]}
    assert facts["What went wrong"] == long                    # nothing cut on the board
    assert facts["What it tried"] == "write_file then git commit"
    assert facts["Chat"] == "chat #646"
    assert "Jav3's own tools" in board["title"]


async def test_short_fault_summary_is_untouched(db):
    await _fault(db, "short")
    ev = [e for e in await security.list_events(db) if e["kind"] == "harness_fault"][0]
    assert ev["summary"] == "Harness fault reported: write_file: short"


async def test_ack_all_can_leave_reports_alone_or_clear_only_them(client, db):
    await _fault(db)
    await security.raise_event(db, kind="desk_refused", severity="warn", summary="x")
    r = await client.post("/api/security/events/ack_all", params={"exclude": "harness_fault"})
    assert r.json()["done"] == 1
    left = [e["kind"] for e in await security.list_events(db, unacknowledged_only=True)]
    assert left == ["harness_fault"]
    await security.raise_event(db, kind="desk_refused", severity="warn", summary="y",
                               cause="y")
    r = await client.post("/api/security/events/ack_all", params={"only": "harness_fault"})
    assert r.json()["done"] == 1
    left = [e["kind"] for e in await security.list_events(db, unacknowledged_only=True)]
    assert left == ["desk_refused"]


# --- WEB-20: the profile's secret checklist knows which secrets are infrastructure

async def test_profile_secret_choices_mark_infrastructure(client, tmp_env):
    from backend import secrets as secrets_store
    secrets_store.save({"NEWS_API_KEY": "n" * 12, "CF_ACCESS_CLIENT_ID": "i" * 12,
                        "CF_ACCESS_CLIENT_SECRET": {"value": "s" * 12, "hosts": ["x.example"]},
                        "GITHUB_TOKEN": "g" * 12, "JAV3_PAIRING": "p" * 12})
    r = await client.get("/api/profiles/secret-choices")
    assert r.status_code == 200
    got = {s["name"]: s["infrastructure"] for s in r.json()["secrets"]}
    assert got == {"CF_ACCESS_CLIENT_ID": True, "CF_ACCESS_CLIENT_SECRET": True,
                   "JAV3_PAIRING": True, "GITHUB_TOKEN": False, "NEWS_API_KEY": False}
    # names only: no value and no tail comes back
    assert "n" * 4 not in r.text and "last4" not in r.text


# --- WEB-02: an alert can be allowed, not only acknowledged --------------------

async def _proc_alert(db, exe="/usr/local/bin/job", unit="weekly-job.service", pid=7,
                      box="shared"):
    return await security.raise_event(
        db, kind="unexpected_process", severity="warn",
        summary=f"Unexpected process in box {box}: {exe}", cause=f"{box}:{exe}:{unit}:{pid}",
        detail={"box_id": box, "pid": pid, "exe": exe, "cmd": f"{exe} changelog",
                "unit": unit, "user": "root", "baseline": "builtin"})


def _box():
    import types
    return types.SimpleNamespace(image=("main", None))


async def test_allow_program_baselines_it_and_acks_its_twins(client, db):
    from backend.vm import procview
    a = await _proc_alert(db, pid=7)
    b = await _proc_alert(db, pid=8)                       # same program, same unit
    c = await _proc_alert(db, exe="/usr/bin/python3", pid=9)   # another program, same unit
    r = await client.post(f"/api/security/events/{a}/baseline", json={"scope": "program"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["acknowledged"] == 2
    base = await procview.baseline_for(db, _box())
    assert base.matches("/usr/local/bin/job", "weekly-job.service")
    assert not base.matches("/usr/bin/python3", "weekly-job.service")   # not the unit
    assert not base.matches("/usr/local/bin/job", "evil.service")
    left = [e["id"] for e in await security.list_events(db, unacknowledged_only=True)
            if e["kind"] == "unexpected_process"]
    assert left == [c] and b != c
    # the change itself is on the audit trail, as a record (no ping)
    audit = [e for e in await security.list_events(db) if e["kind"] == "proc_baseline_changed"]
    assert len(audit) == 1 and audit[0]["severity"] == "info"
    assert audit[0]["acknowledged"]          # a record, not a queue item


async def test_allow_unit_covers_every_program_in_it(client, db):
    from backend.vm import procview
    a = await _proc_alert(db)
    c = await _proc_alert(db, exe="/usr/bin/python3", pid=9)
    r = await client.post(f"/api/security/events/{a}/baseline", json={"scope": "unit"})
    assert r.status_code == 200 and r.json()["acknowledged"] == 2 and c
    base = await procview.baseline_for(db, _box())
    assert base.matches("/usr/bin/anything", "weekly-job.service")
    assert not base.matches("/usr/bin/anything", "other.service")


async def test_allowed_list_can_be_read_and_taken_back(client, db):
    from backend.vm import procview
    a = await _proc_alert(db)
    await client.post(f"/api/security/events/{a}/baseline", json={"scope": "program"})
    got = (await client.get("/api/security/baseline")).json()["entries"]
    assert [(e["exe"], e["unit"], e["by"]) for e in got] \
        == [("/usr/local/bin/job", "weekly-job.service", "operator")]
    r = await client.post("/api/security/baseline/remove",
                          json={"exe": "/usr/local/bin/job", "unit": "weekly-job.service"})
    assert r.status_code == 200
    assert not (await procview.baseline_for(db, _box())).matches(
        "/usr/local/bin/job", "weekly-job.service")
    assert (await client.post("/api/security/baseline/remove",
                              json={"exe": "/usr/local/bin/job",
                                    "unit": "weekly-job.service"})).status_code == 404


@pytest.mark.parametrize("exe,unit,scope,why", [
    ("/usr/bin/python3", "jarvis-guest.service", "program", "guest server"),   # never baselined
    ("/usr/bin/x", "jav3-svc-4.service", "unit", "approved service"),
    ("/usr/bin/python3", "", "program", "interpreter"),                  # would hide any implant
    ("/usr/bin/x", "", "unit", "no unit"),
    ("/usr/bin/*", "a.service", "program", "no patterns"),
    ("", "a.service", "program", "no program"),
])
async def test_allow_refuses_what_would_hide_too_much(client, db, exe, unit, scope, why):
    from backend.vm import procview
    eid = await security.raise_event(
        db, kind="unexpected_process", severity="warn", summary="x",
        detail={"box_id": "shared", "exe": exe, "unit": unit})
    r = await client.post(f"/api/security/events/{eid}/baseline", json={"scope": scope})
    assert r.status_code == 400, why
    assert await procview.operator_entries(db) == []


async def test_allow_only_for_process_alerts(client, db):
    eid = await security.raise_event(db, kind="secret_leak", severity="critical", summary="x")
    assert (await client.post(f"/api/security/events/{eid}/baseline",
                              json={"scope": "program"})).status_code == 400
    assert (await client.post("/api/security/events/99999/baseline",
                              json={"scope": "program"})).status_code == 404


async def test_process_board_names_the_program_and_fills_the_what_column(client, db):
    a = await _proc_alert(db, pid=7)
    await _proc_alert(db, exe="/usr/bin/python3", pid=9)
    board = (await client.get(f"/api/security/events/{a}/context")).json()
    facts = {r["label"]: r for s in board["sections"] if s["type"] == "facts" for r in s["rows"]}
    assert facts["Program"]["value"] == "/usr/local/bin/job"
    assert facts["Started by unit"]["value"] == "weekly-job.service"
    assert "stock Debian" in facts["Baseline"]["hint"]           # 'builtin' explained
    table = [s for s in board["sections"] if s["type"] == "table"][0]
    assert table["rows"][0][2] == "/usr/bin/python3 in weekly-job.service"   # What


# --- WEB-07: "Allow once" is an hour, on no list, and the host comes back ------

async def test_allow_once_is_time_boxed_and_writes_no_list(client, db):
    await db.execute("INSERT INTO projects (slug, name, path) VALUES ('proj','Proj','p')")
    await db.commit()
    await egress.note_denied(db, "proj", "odd.example")
    pid = (await client.get("/api/egress/pending")).json()["pending"][0]["id"]
    r = await client.post(f"/api/egress/pending/{pid}/approve", json={"once": True})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["project"] == "proj"
    # through for now, on the project's own list: no
    assert (await egress.decide(db, "proj", "odd.example"))[0] == "allow"
    pol = (await client.get("/api/egress/policy/proj")).json()
    assert "odd.example" not in pol["project_allow"]
    # off the queue, and one entry in the standing list marked as a one-off
    assert (await client.get("/api/egress/pending")).json()["pending"] == []
    grp = {g["project"]: g for g in (await client.get("/api/egress/allowlist")).json()["groups"]}
    once = [e for e in grp["proj"]["entries"] if e.get("rule") == "once"]
    assert len(once) == 1 and once[0]["source"] == "auto"
    # the operator's one-offs never use up auto mode's daily cap
    assert await egress.auto_allows_today(db, "proj") == 0
    # when the hour is up it is denied and queued again
    await db.execute("UPDATE egress_auto_allow SET expires_at = datetime('now', '-1 minute')")
    await db.commit()
    assert (await egress.decide(db, "proj", "odd.example"))[1] == egress.NOT_LISTED
    await egress.note_denied(db, "proj", "odd.example")
    assert [p["host"] for p in (await client.get("/api/egress/pending")).json()["pending"]] \
        == ["odd.example"]


async def test_allow_once_still_needs_a_project_for_unattributed_rows(client, db):
    await egress.note_denied(db, egress.GENERAL, "odd.example")
    pid = (await client.get("/api/egress/pending")).json()["pending"][0]["id"]
    r = await client.post(f"/api/egress/pending/{pid}/approve", json={"once": True})
    assert r.status_code == 409 and r.json()["detail"] == "needs_project"


# --- WEB-08: the host's own address is not a site you can allow ----------------

@pytest.fixture
def fake_host(monkeypatch):
    import ipaddress
    from backend import lanaccess
    monkeypatch.setattr(lanaccess, "_host_view", lambda: (
        frozenset({"10.0.0.82", "10.201.0.1"}),
        (ipaddress.ip_network("10.201.0.0/16"),)))


@pytest.mark.parametrize("host,kind", [
    ("10.201.0.1", "host"), ("10.0.0.82", "host"), ("127.0.0.1", "host"),
    ("[::1]", "host"), ("10.0.0.60", "lan"), ("192.168.1.10", "lan"),
    ("8.8.8.8", None), ("pypi.org", None), ("nas.lan", None),
])
def test_unreachable_reason(fake_host, host, kind):
    why = egress.unreachable_reason(host)
    assert why is None if kind is None else why == {
        "host": egress.HOST_REFUSED, "lan": egress.PRIVATE_REFUSED}[kind]


def _att():
    return {"project": "proj", "op_id": None, "conversation_id": None, "box_id": "b1",
            "service_id": None, "kind": "project", "peer_ip": None, "peer_port": None}


async def test_proxy_denies_the_gateway_without_queueing_it(db, fake_host):
    from backend.vm import egress_proxy as ep
    v, reason, pin = await ep._authorize_target("10.201.0.1", "80", _att())
    assert v == "deny" and pin is None and reason != egress.NOT_LISTED
    assert "Jav3 host" in reason
    await ep._record("10.201.0.1", "GET", "/", 0, 0, v, reason, _att())
    assert await egress.list_pending(db, "proj") == []
    # an ordinary unlisted site is still queued
    v, reason, _ = await ep._authorize_target("odd.example", "443", _att())
    assert reason == egress.NOT_LISTED


async def test_old_queued_gateway_row_cannot_be_approved(client, db, fake_host):
    await egress.note_denied(db, "proj", "10.201.0.1")       # queued before the fix
    await egress.note_denied(db, "proj", "pypi.org")
    rows = {r["host"]: r for r in (await client.get("/api/egress/pending")).json()["pending"]}
    assert rows["10.201.0.1"]["refused"] == egress.HOST_REFUSED
    assert rows["pypi.org"]["refused"] is None
    r = await client.post(f"/api/egress/pending/{rows['10.201.0.1']['id']}/approve")
    assert r.status_code == 400 and "Jav3 host" in r.json()["detail"]
    # Approve all adds pypi.org and leaves the gateway alone
    r = await client.post("/api/egress/pending/bulk", json={"action": "approve"})
    assert r.json() == {"ok": True, "done": 1, "skipped": 1}
    pol = (await client.get("/api/egress/policy/proj")).json()
    assert "10.201.0.1" not in pol["project_allow"] and "pypi.org" in pol["project_allow"]
