"""Security false alarms, step 1 (SB1, 2026-10-01).

The operator: "per run is at least 100 notifs... I just acknowledge all and then
an actually important one slips through." An event a rule judges to be normal
work is still RECORDED (filed already acknowledged, quiet='rule', the reason in
`rule`) but never pings, never counts and never sits in the Queue. These tests
pair every rule's noise with the real case that must still alert.

This file holds the machinery (rule filing, sessions per device per day, the
standing docker note); the per-rule tests sit beside their rule:
diffgate/writes (test_secrules_writes), procview, anomaly."""
import pytest

from backend import bus, egress, security
from backend import db as db_mod


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    egress._cut.clear()
    security._pings.clear()
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


@pytest.fixture
def feed():
    q = bus.subscribe(security.SECURITY_CHAN)

    def drain():
        got = []
        while not q.empty():
            got.append(q.get_nowait())
        return got
    yield drain
    bus.unsubscribe(security.SECURITY_CHAN, q)


async def _row(db, eid):
    async with db.execute("SELECT * FROM security_events WHERE id = ?", (eid,)) as cur:
        return dict(await cur.fetchone())


async def _rows(db, kind):
    async with db.execute("SELECT * FROM security_events WHERE kind = ? ORDER BY id",
                          (kind,)) as cur:
        return [dict(r) for r in await cur.fetchall()]


def _pings(feed):
    return [e for e in feed() if e.get("type") == "security_event" and e["ping"]]


# --- the machinery ------------------------------------------------------------------

async def test_a_rule_files_the_row_acknowledged_with_its_reason_and_no_ping(db, feed):
    await security.set_notify_level(db, "all")             # the loudest level
    eid = await security.raise_event(
        db, kind="write_flag", severity="warn", project="p", summary="write flag: x",
        rule="file never committed")
    r = await _row(db, eid)
    assert r["acknowledged"] == 1 and r["acknowledged_at"]
    assert r["quiet"] == "rule" and r["rule"] == "file never committed"
    assert r["actor"] is None
    evs = feed()
    assert not [e for e in evs if e["ping"]]
    live = [e for e in evs if e.get("id") == eid][0]
    assert live["acknowledged"] is True and live["quiet"] == "rule"
    # never in the badge or the queue, but in the history, with the reason
    tiers, _ = await security.tier_counts(db)
    assert tiers == {"critical": 0, "approval": 0, "alert": 0, "record": 0}
    assert await security.list_events(db, unacknowledged_only=True) == []
    hist = (await security.list_events(db))[0]
    assert hist["quiet"] == "rule" and hist["rule"] == "file never committed"


async def test_the_same_event_without_a_rule_still_alerts(db, feed):
    await security.set_notify_level(db, "all")
    eid = await security.raise_event(db, kind="write_flag", severity="warn", project="p",
                                     summary="write flag: x")
    r = await _row(db, eid)
    assert r["acknowledged"] == 0 and r["quiet"] is None and r["rule"] is None
    assert len(_pings(feed)) == 1
    assert (await security.count_by_tier(db))["alert"] == 1


async def test_a_rule_never_quiets_a_critical_event(db, feed):
    """An anomaly cut is never routine: the rule is ignored."""
    eid = await security.raise_event(db, kind="egress_anomaly", severity="critical",
                                     project="p", summary="high-entropy host x",
                                     rule="looks like a CDN")
    r = await _row(db, eid)
    assert r["acknowledged"] == 0 and r["quiet"] is None and r["rule"] is None
    assert len(_pings(feed)) == 1


async def test_a_rule_beats_a_kind_the_operator_set_to_ping(db, feed):
    await security.set_prefs(db, kinds={"write_flag": "ping"})
    await security.raise_event(db, kind="write_flag", severity="warn", summary="a",
                               rule="normal work")
    assert not _pings(feed)
    await security.raise_event(db, kind="write_flag", severity="warn", summary="b")
    assert len(_pings(feed)) == 1


async def test_repeats_of_a_ruled_row_count_onto_it(db):
    a = await security.raise_event(db, kind="unexpected_process", severity="warn",
                                   project="p", summary="Unexpected process in box p-x: node",
                                   rule="started by the agent")
    b = await security.raise_event(db, kind="unexpected_process", severity="warn",
                                   project="p", summary="Unexpected process in box p-x: node",
                                   rule="started by the agent")
    assert a == b and (await _row(db, a))["count"] == 2
    # a waiting row of the same words is a different row: the rule's is not it
    c = await security.raise_event(db, kind="unexpected_process", severity="warn",
                                   project="p", summary="Unexpected process in box p-x: node")
    assert c != a and (await _row(db, c))["acknowledged"] == 0


async def test_the_migration_is_idempotent(db):
    await db.execute("ALTER TABLE security_events DROP COLUMN rule")
    await db.commit()
    await db_mod._migrate_secrules(db)
    await db_mod._migrate_secrules(db)
    eid = await security.raise_event(db, kind="write_flag", severity="warn", summary="a",
                                     rule="r")
    assert (await _row(db, eid))["rule"] == "r"


# --- rule 6: browser / computer sessions, once per device per day ------------------------

def _session(kind, device, phase, why=None):
    d = {"device_id": device, "phase": phase}
    if why:
        d["why"] = why
    name = "Mac-Browser" if kind == "browser_session" else "grant-mac-desk"
    what = "browser" if kind == "browser_session" else "computer"
    return dict(kind=kind, severity="info", summary=f"{what} '{name}' "
                f"{'connected' if phase == 'start' else (why or 'disconnected')}", detail=d)


@pytest.mark.parametrize("kind", ["browser_session", "desk_session"])
async def test_a_known_devices_reconnects_are_one_quiet_row_a_day(db, feed, kind):
    first = await security.raise_event(db, **_session(kind, 5, "start"))
    r1 = await _row(db, first)
    assert r1["quiet"] is None and r1["rule"] is None         # never seen: raised in full
    ids = set()
    for i in range(40):
        ids.add(await security.raise_event(
            db, **_session(kind, 5, "stop" if i % 2 else "start", "disconnected" if i % 2 else None)))
    assert len(ids) == 1                                      # 40 reconnects, one row
    q = await _row(db, ids.pop())
    assert q["quiet"] == "rule" and q["acknowledged"] == 1 and q["count"] == 40
    assert "known device" in q["rule"]
    assert len(await _rows(db, kind)) == 2
    assert not _pings(feed)


async def test_a_never_seen_device_is_raised_in_full(db):
    a = await security.raise_event(db, **_session("browser_session", 5, "start"))
    b = await security.raise_event(db, **_session("browser_session", 6, "start"))
    assert a != b
    assert (await _row(db, b))["quiet"] is None
    # the other kind has its own devices
    c = await security.raise_event(db, **_session("desk_session", 5, "start"))
    assert (await _row(db, c))["quiet"] is None


async def test_a_device_that_went_silent_during_a_live_turn_is_raised_in_full(db, monkeypatch):
    await security.raise_event(db, **_session("browser_session", 5, "start"))
    monkeypatch.setattr(security, "_live_turn", lambda: True)
    live = await security.raise_event(db, **_session("browser_session", 5, "stop", "went silent"))
    r = await _row(db, live)
    assert r["quiet"] is None and r["acknowledged"] == 0
    # the same silence with no turn running is the quiet daily row
    monkeypatch.setattr(security, "_live_turn", lambda: False)
    idle = await security.raise_event(db, **_session("browser_session", 5, "stop", "went silent"))
    assert idle != live and (await _row(db, idle))["quiet"] == "rule"


async def test_a_new_day_starts_a_new_row(db, monkeypatch):
    await security.raise_event(db, **_session("browser_session", 5, "start"))
    monkeypatch.setattr(security, "_utcnow", lambda: "2026-10-01 09:00:00")
    a = await security.raise_event(db, **_session("browser_session", 5, "start"))
    monkeypatch.setattr(security, "_utcnow", lambda: "2026-10-02 09:00:00")
    b = await security.raise_event(db, **_session("browser_session", 5, "start"))
    assert a != b


# --- rule 5: docker_weak_isolation, once per box allocation -----------------------------

def _weak(box="p-docker-a", allocation=1759300000):
    return dict(kind="docker_weak_isolation", severity="warn", project="docker-a",
                summary=f"docker box {box}: no gVisor: the container shares the host kernel",
                detail={"box": box, "userns": "none", "oci_runtime": "runc",
                        "allocation": allocation})


async def test_a_boxs_weak_isolation_is_stated_once_per_allocation(db, feed):
    await security.set_prefs(db, kinds={"docker_weak_isolation": "ping"})   # the loudest
    first = await security.raise_event(db, **_weak())
    assert len(_pings(feed)) == 1
    for _ in range(14):                                       # fourteen more box starts
        assert await security.raise_event(db, **_weak()) == first
    assert not _pings(feed)
    assert (await _row(db, first))["count"] == 15
    assert len(await _rows(db, "docker_weak_isolation")) == 1


async def test_a_new_allocation_or_a_new_box_states_it_again(db):
    a = await security.raise_event(db, **_weak())
    b = await security.raise_event(db, **_weak(allocation=1759900000))     # wiped and re-made
    c = await security.raise_event(db, **_weak(box="p-docker-b"))
    assert len({a, b, c}) == 3


async def test_the_standing_note_outlives_the_coalescing_window_and_an_acknowledge(db):
    a = await security.raise_event(db, **_weak())
    await security.acknowledge(db, a)
    await db.execute("UPDATE security_events SET last_seen = datetime('now', '-3 days'), "
                     "created_at = datetime('now', '-3 days') WHERE id = ?", (a,))
    await db.commit()
    assert await security.raise_event(db, **_weak()) == a
