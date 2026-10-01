"""The Review Queue hides record-tier rows (SB1, 2026-10-01).

319 of 742 events in a week were info: an audit line with nothing to do, kept
unacknowledged so they could be found. They stay in the history; the Queue lists
what needs the operator. The cut is made in SQL so the limit counts Queue rows
(a hundred waiting audit lines must not push the one real alert out of the list)."""
import httpx
import pytest

from backend import security
from backend import db as db_mod
from backend.auth import hash_password
from backend.main import app


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    security._pings.clear()
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


@pytest.fixture
async def client(db):
    await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                     ("grindlewalt", hash_password("hunter2")))
    await db.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "grindlewalt", "password": "hunter2"})
        yield c


async def _queue(db, **kw):
    return await security.list_events(db, queue_only=True, **kw)


async def test_the_queue_lists_what_needs_the_operator_and_not_audit_lines(db):
    for i in range(5):
        await security.raise_event(db, kind="browser_session", severity="info",
                                   summary=f"browser 'B{i}' connected", detail={"device_id": i})
    await security.raise_event(db, kind="device_enrolled", severity="info", summary="code login")
    alert = await security.raise_event(db, kind="write_flag", severity="warn", project="p",
                                       summary="write flag: new_import in a.py")
    crit = await security.raise_event(db, kind="egress_anomaly", severity="critical",
                                      summary="high-entropy host x")
    fault = await security.raise_event(db, kind="harness_fault", severity="info",
                                       summary="Harness fault reported: x")
    got = await _queue(db)
    assert sorted(e["id"] for e in got) == sorted([alert, crit, fault])   # fault: its own section
    assert {e["id"]: e["tier"] for e in got} == {alert: "alert", crit: "critical",
                                                 fault: "record"}
    # the history lists everything, and the waiting list is unchanged for other callers
    assert len(await security.list_events(db)) == 9
    assert len(await security.list_events(db, unacknowledged_only=True)) == 9


async def test_the_limit_counts_queue_rows_not_audit_lines(db):
    alert = await security.raise_event(db, kind="write_flag", severity="warn", project="p",
                                       summary="write flag: network_call in a.py")
    for i in range(150):                        # newer, and more than the limit
        await security.raise_event(db, kind="browser_session", severity="info",
                                   summary=f"browser 'B{i}' connected", detail={"device_id": i})
    assert [e["id"] for e in await _queue(db, limit=100)] == [alert]
    assert alert not in [e["id"] for e in await security.list_events(db, limit=100)]


async def test_the_operators_choices_move_a_kind_in_and_out_of_the_queue(db):
    info = await security.raise_event(db, kind="persist_revoked", severity="info", summary="i")
    warn = await security.raise_event(db, kind="persist_unplug_failed", severity="warn",
                                      summary="w")
    assert [e["id"] for e in await _queue(db)] == [warn]
    await security.set_prefs(db, kinds={"persist_revoked": "badge",        # counted: so listed
                                        "persist_unplug_failed": "record"})  # a record now
    got = {e["id"]: e["tier"] for e in await _queue(db)}
    assert got == {info: "alert"}
    tiers, _ = await security.tier_counts(db)
    assert tiers["alert"] == 1 and tiers["record"] == 1                    # as the badge reads


async def test_the_route_takes_queue(client, db):
    await security.raise_event(db, kind="browser_session", severity="info", summary="b",
                               detail={"device_id": 1})
    alert = await security.raise_event(db, kind="write_flag", severity="warn", summary="w")
    r = await client.get("/api/security/events?unacknowledged=true&queue=true")
    assert [e["id"] for e in r.json()["events"]] == [alert]
    r = await client.get("/api/security/events?unacknowledged=true")
    assert len(r.json()["events"]) == 2
    r = await client.get("/api/security/events?limit=10")           # the history
    assert len(r.json()["events"]) == 2 and "rule" in r.json()["events"][0]


async def test_a_live_event_says_which_mode_it_is_in(db):
    from backend import bus
    q = bus.subscribe(security.SECURITY_CHAN)
    try:
        await security.raise_event(db, kind="persist_revoked", severity="info", summary="i")
        await security.raise_event(db, kind="write_flag", severity="warn", summary="w")
        evs = [q.get_nowait() for _ in range(2)]
    finally:
        bus.unsubscribe(security.SECURITY_CHAN, q)
    assert [(e["tier"], e["mode"]) for e in evs] == [("record", "record"), ("alert", "badge")]
