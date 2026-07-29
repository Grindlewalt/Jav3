"""Per-project egress policy engine, anomaly detectors, and the security-event
store — the pure-Python core of Layer 3 (no VM / KVM needed)."""
import pytest

from backend import anomaly, egress, security
from backend import db as db_mod
from backend.config import settings


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    egress._cut.clear()
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


# --- policy resolution -------------------------------------------------------

async def test_general_seeded_from_config(db):
    pol = await egress.get_policy(db, "anyproj")
    assert pol["source"] == "general"
    assert "pypi.org" in pol["effective"]           # seeded dev-infra
    assert "evil.example" not in pol["effective"]


async def test_seeded_host_allowed_new_host_denied(db):
    v, _ = await egress.decide(db, "proj", "files.pythonhosted.org")
    assert v == "allow"
    v, reason = await egress.decide(db, "proj", "attacker.xyz")
    assert v == "deny" and "allowlist" in reason


async def test_subdomain_match(db):
    # a subdomain of a seeded host is allowed; a lookalike is not
    assert (await egress.decide(db, "p", "files.pythonhosted.org"))[0] == "allow"
    assert (await egress.decide(db, "p", "pythonhosted.org.evil.com"))[0] == "deny"


async def test_denied_host_queues_and_trains_up(db):
    await egress.decide(db, "proj", "newapi.com")
    await egress.note_denied(db, "proj", "newapi.com")
    pending = await egress.list_pending(db)
    assert len(pending) == 1 and pending[0]["host"] == "newapi.com"
    res = await egress.approve_host(db, pending[0]["id"])
    assert res["ok"] and res["added_to"] == egress.GENERAL
    # now it's allowed, and the queue is clear
    assert (await egress.decide(db, "proj", "newapi.com"))[0] == "allow"
    assert await egress.list_pending(db) == []


async def test_note_denied_bumps_hit_count(db):
    await egress.note_denied(db, "proj", "x.com")
    await egress.note_denied(db, "proj", "x.com")
    assert (await egress.list_pending(db, "proj"))[0]["hit_count"] == 2


async def test_bulk_approve_trains_every_host(db):
    for h in ("a.com", "b.com"):
        await egress.note_denied(db, "proj", h)
    res = await egress.bulk_pending(db, "approve")
    assert res["ok"] and res["done"] == 2
    assert (await egress.decide(db, "proj", "a.com"))[0] == "allow"
    assert (await egress.decide(db, "proj", "b.com"))[0] == "allow"
    assert await egress.list_pending(db) == []


async def test_bulk_dismiss_clears_without_verdict_and_revives(db):
    await egress.note_denied(db, "proj", "later.com")
    res = await egress.bulk_pending(db, "dismiss")
    assert res["ok"] and res["done"] == 1
    assert await egress.list_pending(db) == []
    # nothing was trained — the host is still denied…
    assert (await egress.decide(db, "proj", "later.com"))[0] == "deny"
    # …and a fresh hit re-queues it, same as a rejected host
    await egress.note_denied(db, "proj", "later.com")
    assert (await egress.list_pending(db))[0]["host"] == "later.com"


async def test_bulk_scoped_to_one_project(db):
    await egress.note_denied(db, "projA", "a.com")
    await egress.note_denied(db, "projB", "b.com")
    res = await egress.bulk_pending(db, "reject", "projA")
    assert res["done"] == 1
    left = await egress.list_pending(db)
    assert len(left) == 1 and left[0]["project_slug"] == "projB"


async def test_bulk_rejects_unknown_action(db):
    assert not (await egress.bulk_pending(db, "nuke"))["ok"]


async def test_acknowledge_all(db):
    for i in range(3):
        await security.raise_event(db, kind="test", severity="warn",
                                   summary=f"e{i}")
    res = await security.acknowledge_all(db)
    assert res["ok"] and res["done"] == 3
    assert await security.count_unacknowledged(db) == 0


# --- scoped (sensitive) project policies -------------------------------------

async def test_denyall_project_is_netless(db):
    await egress.set_policy(db, "secret-proj", mode="denyall")
    v, reason = await egress.decide(db, "secret-proj", "pypi.org")
    assert v == "deny" and "disabled" in reason           # even a seeded host is blocked


async def test_scoped_allowlist_does_not_inherit_general(db):
    await egress.set_policy(db, "scoped", mode="allowlist",
                            inherit_general=False, hosts=["internal.api"])
    assert (await egress.decide(db, "scoped", "internal.api"))[0] == "allow"
    assert (await egress.decide(db, "scoped", "pypi.org"))[0] == "deny"   # general NOT inherited
    # approving trains up the SCOPED list, not general
    await egress.note_denied(db, "scoped", "other.api")
    pid = (await egress.list_pending(db, "scoped"))[0]["id"]
    assert (await egress.approve_host(db, pid))["added_to"] == "scoped"


async def test_denylist_mode_allow_by_default(db):
    await egress.set_policy(db, "trusted", mode="denylist", hosts=["ads.tracker"])
    assert (await egress.decide(db, "trusted", "anything.com"))[0] == "allow"
    assert (await egress.decide(db, "trusted", "ads.tracker"))[0] == "deny"


# --- auto-cut ----------------------------------------------------------------

async def test_cut_host_short_circuits(db):
    assert (await egress.decide(db, "proj", "pypi.org"))[0] == "allow"
    egress.mark_cut("proj", "pypi.org")
    assert (await egress.decide(db, "proj", "pypi.org"))[0] == "cut"
    egress.clear_cut("proj", "pypi.org")
    assert (await egress.decide(db, "proj", "pypi.org"))[0] == "allow"


# --- anomaly detectors -------------------------------------------------------

def test_entropy_flags_random_hosts():
    assert anomaly.entropy_bits_per_char("github.com") < settings.egress_entropy_threshold
    assert anomaly.entropy_bits_per_char("x7k2q9v3zp1w8m4t.evil") >= settings.egress_entropy_threshold


async def test_high_entropy_host_trips(db):
    a = await anomaly.check_host(db, "proj", "a8f3k2q9zp1w7v4m.net")
    assert a and a["kind"] == "high_entropy"


async def test_normal_host_no_anomaly(db):
    await egress.record_event(db, slug="proj", host="github.com", bytes_out=500, verdict="allow")
    assert await anomaly.check_host(db, "proj", "github.com") is None


async def test_volume_spike_trips(db):
    # a quiet baseline of small transfers, then one huge dump to a new host
    for h in ("a.com", "b.com", "c.com"):
        await egress.record_event(db, slug="proj", host=h, bytes_out=1000, verdict="allow")
    await egress.record_event(db, slug="proj", host="dump.com",
                              bytes_out=50_000_000, verdict="allow")
    a = await anomaly.check_host(db, "proj", "dump.com")
    assert a and a["kind"] == "volume_spike"


async def test_beacon_cadence_trips(db):
    # insert regularly-spaced events by hand (10s apart) for one host
    base = "2026-07-19 12:00:"
    for i in range(8):
        await db.execute(
            "INSERT INTO egress_events(project_slug, host, bytes_out, verdict, created_at) "
            "VALUES ('proj','c2.example',100,'allow',?)", (f"{base}{i*10:02d}",))
    await db.commit()
    a = await anomaly.check_host(db, "proj", "c2.example")
    assert a and a["kind"] == "beacon_cadence"


# --- security events ---------------------------------------------------------

async def test_security_event_roundtrip(db):
    eid = await security.raise_event(db, kind="host_cut", summary="cut evil.com",
                                     severity="critical", project="proj",
                                     detail={"host": "evil.com"})
    assert await security.count_unacknowledged(db) == 1
    events = await security.list_events(db, unacknowledged_only=True)
    assert events[0]["id"] == eid and events[0]["detail"]["host"] == "evil.com"
    await security.acknowledge(db, eid)
    assert await security.count_unacknowledged(db) == 0
