"""Security alert settings: the operator's own actions, a mode per kind,
do-not-disturb, and critical meaning critical (S1, 2026-09-30).

The operator's complaint: "if I acknowledge something, I know it happened, I
was right there to click it". So an event their own click causes is recorded
already acknowledged ("by you") and never pings or counts; the same kind of
change made by an agent still alerts. Then a Ping / Badge / Record-only choice
per kind, a do-not-disturb with one summary at its end, and an audit of what may
be critical at all."""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from backend import bus, egress, operator_ask, profiles, runtime, security
from backend import db as db_mod
from backend.auth import hash_password
from backend.main import app

REPO = Path(__file__).resolve().parent.parent


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
    """Events published on the security channel since the last call."""
    q = bus.subscribe(security.SECURITY_CHAN)

    def drain():
        """What arrived since the last call."""
        got = []
        while not q.empty():
            got.append(q.get_nowait())
        return got
    yield drain
    bus.unsubscribe(security.SECURITY_CHAN, q)


@pytest.fixture
async def client(db):
    await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                     ("grindlewalt", hash_password("hunter2")))
    await db.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "grindlewalt", "password": "hunter2"})
        yield c


async def _row(db, eid):
    async with db.execute("SELECT * FROM security_events WHERE id = ?", (eid,)) as cur:
        return dict(await cur.fetchone())


async def _rows(db, kind):
    async with db.execute("SELECT * FROM security_events WHERE kind = ? ORDER BY id",
                          (kind,)) as cur:
        return [dict(r) for r in await cur.fetchall()]


def _pings(feed):
    return [e for e in feed() if e.get("type") == "security_event" and e["ping"]]


# --- 1. the operator's own actions ------------------------------------------------

async def test_an_operator_marked_event_is_filed_acknowledged_and_never_pings(db, feed):
    await security.set_notify_level(db, "all")             # the loudest level
    eid = await security.raise_event(db, kind="profile_changed", severity="warn",
                                     summary="profile 'A' deleted by grindlewalt",
                                     actor="operator")
    r = await _row(db, eid)
    assert r["acknowledged"] == 1 and r["acknowledged_at"] and r["actor"] == "operator"
    assert r["quiet"] == "operator"
    evs = feed()
    assert not [e for e in evs if e["ping"]]
    live = [e for e in evs if e.get("id") == eid][0]
    assert live["acknowledged"] is True and live["actor"] == "operator"
    # never in the badge, nor in what would ping
    tiers, pinging = await security.tier_counts(db)
    assert tiers == {"critical": 0, "approval": 0, "alert": 0, "record": 0}
    assert pinging["alert"] == 0
    assert (await security.list_events(db, unacknowledged_only=True)) == []
    # ...and it is in the history, tagged
    assert (await security.list_events(db))[0]["actor"] == "operator"


async def test_the_same_event_unmarked_still_alerts(db, feed):
    """The agent's version of the same change: not marked, so it alerts."""
    await security.set_notify_level(db, "all")
    eid = await security.raise_event(db, kind="profile_changed", severity="warn",
                                     summary="profile 'A' deleted by the server")
    r = await _row(db, eid)
    assert r["acknowledged"] == 0 and r["actor"] is None and r["quiet"] is None
    assert len(_pings(feed)) == 1
    assert (await security.count_by_tier(db))["alert"] == 1


async def test_only_the_explicit_operator_value_counts(db):
    for who in ("grindlewalt", "agent", "OPERATOR", "", "root"):
        eid = await security.raise_event(db, kind="lan_access_changed", severity="warn",
                                         summary=f"by {who}", actor=who)
        r = await _row(db, eid)
        assert r["acknowledged"] == 0 and r["actor"] is None, who


async def test_an_ambient_operator_request_does_not_make_an_agent_look_like_the_operator(db):
    """A chat turn starts from the operator's HTTP request and its task inherits
    the request's context. Nothing may be read from that: the agent's tool path
    reaches the same function without the explicit mark and still alerts."""
    p = await profiles.create(db, {"name": "Wide", "default_verdict": "allow",
                                   "service_placement": "per_project", "box_runtime": "kvm"},
                              by_operator=True)
    tokens = (runtime.web_session.set("operator-request"), runtime.conversation_id.set(7))

    async def the_agent_tool_path():                  # inherits the contexts set above
        assert runtime.web_session.get() == "operator-request"
        return await profiles.update(db, p["id"], {"service_placement": "per_project",
                                                   "box_runtime": "kvm", "name": "Wider"})
    try:
        await asyncio.create_task(the_agent_tool_path())
    finally:
        runtime.conversation_id.reset(tokens[1])
        runtime.web_session.reset(tokens[0])
    rows = await _rows(db, "profile_changed")
    mine, theirs = rows[-2], rows[-1]       # (the first row is the default made on first use)
    assert mine["acknowledged"] == 1 and mine["actor"] == "operator"      # the click
    assert theirs["acknowledged"] == 0 and theirs["actor"] is None       # the tool path


async def test_critical_is_never_quieted_by_an_operator_mark(db, feed):
    eid = await security.raise_event(db, kind="write_flag", severity="critical",
                                     summary="a secret", actor="operator")
    r = await _row(db, eid)
    assert r["acknowledged"] == 0 and r["quiet"] is None
    assert len(_pings(feed)) == 1


async def test_the_toggle_off_keeps_the_tag_but_alerts(db, feed):
    await security.set_prefs(db, self_quiet=False)
    await security.set_notify_level(db, "all")
    eid = await security.raise_event(db, kind="profile_changed", severity="warn",
                                     summary="mine", actor="operator")
    r = await _row(db, eid)
    assert r["acknowledged"] == 0 and r["actor"] == "operator" and r["quiet"] is None
    assert len(_pings(feed)) == 1


async def test_operator_actions_are_one_row_each_and_do_not_join_an_agents_twin(db):
    a = await security.raise_event(db, kind="lan_access_changed", severity="warn",
                                   summary="LAN on for demo")                     # an agent's
    b = await security.raise_event(db, kind="lan_access_changed", severity="warn",
                                   summary="LAN on for demo", actor="operator")
    c = await security.raise_event(db, kind="lan_access_changed", severity="warn",
                                   summary="LAN on for demo", actor="operator")
    assert len({a, b, c}) == 3
    assert (await _row(db, a))["count"] == 1 and (await _row(db, a))["acknowledged"] == 0


async def test_the_routes_mark_the_click_and_a_direct_call_is_not_one(client, db):
    """PUT /api/profiles/{id} is the operator's click; the same edit made by
    calling profiles.update (the reviewer's undo, an agent path) is not."""
    d = await profiles.default(db)
    body = {"name": "Home", "service_placement": "per_project", "box_runtime": "kvm"}
    r = await client.put(f"/api/profiles/{d['id']}", json=body)
    assert r.status_code == 200
    click = (await _rows(db, "profile_changed"))[-1]
    assert click["actor"] == "operator" and click["acknowledged"] == 1
    assert "by grindlewalt" in click["summary"]
    await profiles.update(db, d["id"], {**body, "name": "Home2"})
    other = (await _rows(db, "profile_changed"))[-1]
    assert other["actor"] is None and other["acknowledged"] == 0
    assert "by the server" in other["summary"]


async def test_operator_widening_is_a_quiet_warning_and_an_unmarked_one_is_critical(client, db):
    wide = {"default_verdict": "allow", "service_placement": "per_project", "box_runtime": "kvm"}
    r = await client.post("/api/profiles", json={"name": "Wide", **wide})
    assert r.status_code == 200
    mine = (await _rows(db, "profile_changed"))[-1]
    assert mine["severity"] == "warn" and mine["acknowledged"] == 1
    await profiles.create(db, {"name": "Wide2", **wide}, actor="some tool")
    theirs = (await _rows(db, "profile_changed"))[-1]
    assert theirs["severity"] == "critical" and theirs["acknowledged"] == 0


async def test_revoking_a_host_from_the_page_is_the_operators_click(client, db):
    p = await profiles.create(db, {"name": "P", "allow_hosts": ["a.dev", "b.dev"],
                                   "service_placement": "per_project", "box_runtime": "kvm"})
    r = await client.post("/api/egress/allowlist/revoke",
                          json={"project": f"profile:{p['id']}", "host": "a.dev"})
    assert r.status_code == 200, r.text
    assert (await _rows(db, "profile_changed"))[-1]["actor"] == "operator"
    # the automatic reviewer's undo goes through the same function, unmarked
    await egress.remove_host(db, f"profile:{p['id']}", "b.dev")
    assert (await _rows(db, "profile_changed"))[-1]["actor"] is None


async def test_lan_access_from_the_page_is_the_operators_click(client, db):
    await db.execute("INSERT INTO projects (slug, name, path) VALUES ('demo', 'Demo', '/tmp/demo')")
    await db.commit()
    r = await client.put("/api/egress/lan/demo", json={"enabled": True, "allow": ["10.0.0.0/24"]})
    assert r.status_code == 200, r.text
    ev = (await _rows(db, "lan_access_changed"))[-1]
    assert ev["actor"] == "operator" and ev["acknowledged"] == 1
    assert (await client.get("/api/notifications")).json()["count"] == 0


# --- 2. a mode per kind -----------------------------------------------------------

async def test_defaults_are_what_happened_before(db, feed):
    """No choice made: a warn kind pings at 'all' only, and an info row is a
    record that stays out of the badge."""
    await security.raise_event(db, kind="unexpected_process", severity="warn", summary="u")
    await security.raise_event(db, kind="local_session", severity="info", summary="l")
    assert not _pings(feed)                                  # default level: approvals
    tiers, pinging = await security.tier_counts(db)
    assert tiers["alert"] == 1 and tiers["record"] == 1 and pinging["alert"] == 0
    await security.set_notify_level(db, "all")
    tiers, pinging = await security.tier_counts(db)
    assert pinging["alert"] == 1 and pinging["record"] == 0
    feed()
    await security.raise_event(db, kind="unexpected_process", severity="warn", summary="u2")
    assert len(_pings(feed)) == 1


async def test_docker_weak_isolation_is_record_only_by_default(db, feed):
    await security.set_notify_level(db, "all")
    eid = await security.raise_event(db, kind="docker_weak_isolation", severity="warn",
                                     project="docker-a", summary="docker box a: no gVisor")
    r = await _row(db, eid)
    assert r["acknowledged"] == 1 and r["quiet"] == "kind" and r["actor"] is None
    assert not _pings(feed)
    assert (await security.count_by_tier(db))["alert"] == 0
    # every box start is the same standing note: one quiet row, counted
    again = await security.raise_event(db, kind="docker_weak_isolation", severity="warn",
                                       project="docker-a", summary="docker box a: no gVisor")
    assert again == eid and (await _row(db, eid))["count"] == 2


async def test_the_operator_can_make_a_kind_ping_badge_or_record(db, feed):
    await security.set_prefs(db, kinds={"unexpected_process": "ping"})
    await security.raise_event(db, kind="unexpected_process", severity="warn", summary="u1")
    assert len(_pings(feed)) == 1                             # pings at the default level

    await security.set_prefs(db, kinds={"unexpected_process": "badge"})
    await security.set_notify_level(db, "all")                # the level would ping it
    feed()
    await security.raise_event(db, kind="unexpected_process", severity="warn", summary="u2")
    assert not _pings(feed)
    tiers, pinging = await security.tier_counts(db)
    assert tiers["alert"] == 2 and pinging["alert"] == 0      # the mode applies to what waits

    await security.set_prefs(db, kinds={"unexpected_process": "record"})
    eid = await security.raise_event(db, kind="unexpected_process", severity="warn",
                                     summary="u3")
    assert (await _row(db, eid))["acknowledged"] == 1
    assert (await security.count_by_tier(db))["record"] == 2   # u1, u2 wait but count as records

    await security.set_prefs(db, kinds={"unexpected_process": None})      # back to the level
    assert "unexpected_process" not in (await security.get_prefs(db))["kinds"]


async def test_an_info_kind_set_to_badge_counts(db):
    await security.set_prefs(db, kinds={"browser_session": "badge"})
    await security.raise_event(db, kind="browser_session", severity="info", summary="b")
    tiers, pinging = await security.tier_counts(db)
    assert tiers["alert"] == 1 and pinging["alert"] == 0


async def test_a_critical_row_ignores_its_kinds_mode(db, feed):
    await security.set_prefs(db, kinds={"write_flag": "record"})
    eid = await security.raise_event(db, kind="write_flag", severity="critical", summary="s")
    assert (await _row(db, eid))["acknowledged"] == 0
    assert len(_pings(feed)) == 1


async def test_modes_are_validated_server_side(client, db):
    ok = await client.put("/api/notifications/settings",
                          json={"kinds": {"profile_changed": "badge"}})
    assert ok.status_code == 200
    row = [k for k in ok.json()["kinds"] if k["kind"] == "profile_changed"][0]
    assert row["mode"] == "badge" and row["chosen"] is True and row["default"] == "badge"
    for bad in ({"no_such_kind": "ping"},                      # unknown kind
                {"profile_changed": "loud"},                   # unknown mode
                {"egress_anomaly": "record"}):                 # locked: always pings
        r = await client.put("/api/notifications/settings", json={"kinds": bad})
        assert r.status_code == 400, bad
    assert (await security.get_prefs(db))["kinds"] == {"profile_changed": "badge"}


async def test_a_kind_already_in_the_log_can_be_set(client, db):
    await security.raise_event(db, kind="brand_new_kind", severity="warn", summary="x")
    r = await client.put("/api/notifications/settings",
                         json={"kinds": {"brand_new_kind": "record"}})
    assert r.status_code == 200


async def test_the_settings_view_lists_defaults_and_locks(client, db):
    got = (await client.get("/api/notifications/settings")).json()
    assert got["self_quiet"] is True and got["dnd_break_critical"] is True
    assert got["dnd"]["on"] is False and got["modes"] == ["ping", "badge", "record"]
    by = {k["kind"]: k for k in got["kinds"]}
    assert by["docker_weak_isolation"]["mode"] == "record" and not by["docker_weak_isolation"]["chosen"]
    assert by["provider_balance"]["mode"] == "ping"
    assert by["unexpected_process"]["mode"] == "badge"        # level 'approvals': badge, no ping
    for locked in ("egress_anomaly", "host_cut", "secret_leak", "proc_report_mismatch",
                   "skill_pin_mismatch"):
        assert by[locked]["locked"] and by[locked]["mode"] == "ping"
    assert not by["profile_changed"]["locked"]
    # the level moves every kind that has no choice of its own
    await client.put("/api/notifications/settings", json={"level": "all"})
    by = {k["kind"]: k for k in (await client.get("/api/notifications/settings")).json()["kinds"]}
    assert by["unexpected_process"]["mode"] == "ping"
    assert by["docker_weak_isolation"]["mode"] == "record"    # a named default stays


async def test_settings_changes_persist_with_the_level(client, db):
    r = await client.put("/api/notifications/settings",
                         json={"level": "critical", "self_quiet": False,
                               "dnd_break_critical": False})
    assert r.status_code == 200 and r.json()["level"] == "critical"
    prefs = await security.get_prefs(db)
    assert prefs["self_quiet"] is False and prefs["dnd_break_critical"] is False


async def test_garbage_prefs_read_as_the_defaults(db):
    from backend.db import set_state
    await set_state(db, security.PREFS_KEY, "{not json")
    assert await security.get_prefs(db) == {"self_quiet": True, "dnd_break_critical": True,
                                            "kinds": {}}
    await set_state(db, security.PREFS_KEY, '{"kinds": {"a": "loud", "b": "badge"}, "self_quiet": 3}')
    assert (await security.get_prefs(db))["kinds"] == {"b": "badge"}


# --- 3. do not disturb ------------------------------------------------------------

async def test_dnd_silences_pings_but_not_the_badge_or_the_record(db, client, feed):
    await security.set_notify_level(db, "all")
    await security.set_dnd(db, True)
    eid = await security.raise_event(db, kind="unexpected_process", severity="warn",
                                     summary="u")
    assert not _pings(feed)                                   # no toast, no sidebar ping
    assert (await _row(db, eid))["acknowledged"] == 0         # still recorded, still waiting
    body = (await client.get("/api/notifications")).json()
    assert body["count"] == 1 and body["alerts"] == 1         # the badge counts
    assert body["ping_count"] == 0 and body["dnd"]["on"] is True


async def test_dnd_lets_critical_through_unless_told_not_to(db, client, feed):
    await security.set_dnd(db, True)
    await security.raise_event(db, kind="egress_anomaly", severity="critical", summary="c1")
    assert len(_pings(feed)) == 1                             # breaks through (the default)
    assert (await client.get("/api/notifications")).json()["ping_count"] == 1
    await security.set_prefs(db, dnd_break_critical=False)
    feed()
    await security.raise_event(db, kind="egress_anomaly", severity="critical", summary="c2")
    assert not _pings(feed)
    body = (await client.get("/api/notifications")).json()
    assert body["ping_count"] == 0 and body["critical"] == 2  # recorded and counted


async def test_dnd_holds_a_critical_repeat_too_when_it_may_not_break_through(db, feed):
    await security.set_prefs(db, dnd_break_critical=False)
    await security.set_dnd(db, True)
    a = await security.raise_event(db, kind="egress_anomaly", severity="critical", summary="c")
    b = await security.raise_event(db, kind="egress_anomaly", severity="critical", summary="c")
    assert a == b and not _pings(feed)


async def test_a_waiting_ask_stays_visible_under_dnd_and_the_turn_still_waits(db, client):
    await security.set_dnd(db, True)
    t = (runtime.conversation_id.set(1), runtime.event_chan.set("chat:1"))
    try:
        task = asyncio.create_task(operator_ask.ask(
            operator_ask.clean_questions([{"question": "Which?", "options": ["a", "b"]}])))
        for _ in range(100):
            if operator_ask.pending_list():
                break
            await asyncio.sleep(0.01)
    finally:
        runtime.conversation_id.reset(t[0])
        runtime.event_chan.reset(t[1])
    body = (await client.get("/api/notifications")).json()
    assert len(body["asks"]) == 1                             # the item stays visible
    assert body["count"] == 1 and body["ping_count"] == 0     # only its ping is quiet
    assert not task.done()                                    # the turn still waits for the answer
    operator_ask.cancel_all()
    await asyncio.gather(task, return_exceptions=True)


async def test_ending_dnd_sends_one_summary(db, feed):
    await security.set_notify_level(db, "all")
    await security.set_dnd(db, True)
    feed()
    await security.raise_event(db, kind="unexpected_process", severity="warn", summary="u1")
    await security.raise_event(db, kind="gateway_cap", severity="warn", summary="g1")
    await security.raise_event(db, kind="service_requested", severity="info", summary="s1")
    await security.raise_event(db, kind="local_session", severity="info", summary="rec")
    out = await security.set_dnd(db, False)
    assert out["on"] is False
    ev = feed()
    sums = [e for e in ev if e["type"] == "dnd_summary"]
    assert len(sums) == 1 and sums[0]["ping"] is True
    # 2 warn alerts + the service request (an approval-tier row) would have pinged
    assert sums[0]["alerts"] == 3
    assert sums[0]["summary"].startswith("While you were in do-not-disturb: 3 alerts")
    assert [e for e in ev if e["type"] == "dnd_changed"][-1]["on"] is False
    # turning it off again says nothing
    feed()
    await security.set_dnd(db, False)
    assert not [e for e in feed() if e["type"] == "dnd_summary"]


async def _backdate_dnd(db):
    """Make a running do-not-disturb's end a time already gone."""
    import json
    from backend.db import get_state, set_state
    state = json.loads(await get_state(db, security.DND_KEY))
    state["until"] = "2020-01-01T00:00:00Z"
    await set_state(db, security.DND_KEY, json.dumps(state))


async def test_an_expired_dnd_ends_once_whoever_looks_first(db, feed):
    await security.set_notify_level(db, "all")
    await security.set_dnd(db, True)
    await security.raise_event(db, kind="unexpected_process", severity="warn", summary="u")
    await _backdate_dnd(db)
    feed()
    first = await security.dnd_status(db)
    second = await security.dnd_status(db)
    assert first["on"] is False and second["on"] is False
    assert len([e for e in feed() if e["type"] == "dnd_summary"]) == 1
    # and a new event pings again
    await security.raise_event(db, kind="unexpected_process", severity="warn", summary="u2")
    assert len(_pings(feed)) == 1


async def test_the_timers_callback_ends_a_timed_dnd(db, feed, monkeypatch):
    monkeypatch.setattr(security, "_arm_dnd_timer", lambda until: None)   # no real clock
    await security.set_notify_level(db, "all")
    await security.raise_event(db, kind="unexpected_process", severity="warn", summary="u")
    soon = security._iso(datetime.now(timezone.utc) + timedelta(days=1))
    out = await security.set_dnd(db, True, until=soon)
    assert out["on"] is True and out["until"] == soon
    await security._dnd_expired()                             # not yet: nothing happens
    assert (await security.dnd_status(db))["on"] is True
    await _backdate_dnd(db)
    feed()
    await security._dnd_expired()                             # what the timer runs
    assert (await security.dnd_status(db))["on"] is False
    assert len([e for e in feed() if e["type"] == "dnd_summary"]) == 0   # nothing came in during it


async def test_dnd_end_must_be_in_the_future_and_not_too_far(db):
    for bad in ("2020-01-01T00:00:00Z", "tomorrow", "2999-01-01T00:00:00Z"):
        with pytest.raises(ValueError):
            await security.set_dnd(db, True, until=bad)
    assert (await security.dnd_status(db))["on"] is False


async def test_the_dnd_endpoints(client, db, feed):
    r = await client.put("/api/notifications/dnd", json={"on": True, "minutes": 60})
    assert r.status_code == 200 and r.json()["on"] is True and r.json()["until"]
    assert (await client.get("/api/notifications/dnd")).json()["on"] is True
    assert (await client.get("/api/notifications")).json()["dnd"]["on"] is True
    assert (await client.put("/api/notifications/dnd",
                             json={"on": True, "minutes": 0})).status_code == 400
    assert (await client.put("/api/notifications/dnd",
                             json={"on": True, "until": "nonsense"})).status_code == 400
    assert any(e["type"] == "dnd_changed" and e["on"] for e in feed())
    r = await client.put("/api/notifications/dnd", json={"on": False})
    assert r.status_code == 200 and r.json()["on"] is False


# --- 4. critical means critical ----------------------------------------------------

async def test_an_empty_provider_account_is_a_warning_that_still_pings(db, feed):
    """Demoted from critical: an outage, not a breach. It must not break through
    do-not-disturb, but it still pings at the default level (no one is watching
    a scheduled run)."""
    from backend import provider_balance
    provider_balance.reset()
    await provider_balance._notice("deepseek", "DeepSeek balance is empty", "{}")
    ev = (await _rows(db, "provider_balance"))[-1]
    assert ev["severity"] == "warn" and security.tier("provider_balance", "warn") == "alert"
    assert len(_pings(feed)) == 1                             # default mode: ping
    assert not security.locked("provider_balance")
    provider_balance.reset()
    # ...and it does not break through do-not-disturb
    await security.set_dnd(db, True)
    feed()
    await provider_balance._notice("openai", "OpenAI balance is empty", "{}")
    assert not _pings(feed)


async def test_setup_choosing_a_wide_default_is_not_critical(db):
    ch = {"default_verdict": "allow", "service_placement": "per_project", "box_runtime": "kvm"}
    p = await profiles.create(db, {"name": "S", **ch}, actor="setup", make_default=True)
    assert (await _rows(db, "profile_changed"))[-1]["severity"] == "warn"
    assert p["is_default"]


# every place that can raise the critical tier, as audited: a new one needs a
# reason, which means editing this table and the audit in the report
CRITICAL_RAISERS = {
    "backend/agent/tools/imported.py": 1,     # skill_pin_mismatch: a pinned skill drifted
    "backend/vm/egress_proxy.py": 1,          # egress_anomaly: a host cut
    "backend/vm/procview.py": 1,              # unexpected_process on a service box
    "backend/writes.py": 1,                   # a secret value refused in a write
    "backend/profiles.py": 1,                 # the unmarked widening in _severity
}


def test_the_critical_raisers_are_the_audited_ones():
    found = {}
    for path in sorted((REPO / "backend").rglob("*.py")):
        rel = str(path.relative_to(REPO))
        if rel == "backend/security.py":
            continue
        n = len(re.findall(r"""severity\s*=\s*["']critical["']|["']severity["']\s*:\s*[^,\n]*["']critical["']|
                               ["']critical["']\s+if\s+""", path.read_text(), re.X))
        n += len(re.findall(r'else "critical"', path.read_text()))
        if n:
            found[rel] = n
    assert found == CRITICAL_RAISERS, found
    # the kinds that always ping whatever severity they carry
    assert security.ALWAYS_KINDS == {"egress_anomaly", "host_cut", "secret_leak",
                                     "proc_report_mismatch"}
