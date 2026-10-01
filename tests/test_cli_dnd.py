"""The terminal client's do-not-disturb (S1, 2026-09-30): `/dnd`, `/dnd 1h`,
`/dnd off`, a DND mark in the status row while it is on, and pings that stay
quiet meanwhile. The server decides what pings and stamps each live event, so
the client only holds back its own notices (an agent finishing, a storage
warning) and lets through what the server marks: a critical alert allowed to
break through, and the one summary when do-not-disturb ends."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from cli_fake import Feed, FakeServer, load_client, pin_zone, wait_for

jav3 = load_client("jav3cli_dnd")
FULL = jav3.SESSION_PREFIX + "jwt"


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    yield from pin_zone(monkeypatch)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path / "cfg" / "jav3"


class DndServer(FakeServer):
    """The chat server plus the notice stream and /api/notifications/dnd."""

    def __init__(self, dnd: dict | None = None) -> None:
        super().__init__(full=True)
        self.dnd = dnd or {"on": False, "since": None, "until": None, "break_critical": True}
        self.puts: list[dict] = []
        self.streams_open: list[Feed] = []

    def say(self, event: dict, topic: str = "security") -> None:
        self.streams_open[-1].put({"topic": topic, "event": event})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/api/events":
            feed = Feed()
            feed.put({"topic": "security", "event": {"type": "stream_open"}})
            self.streams_open.append(feed)
            return httpx.Response(200, stream=feed,
                                  headers={"content-type": "text/event-stream"})
        if path == "/api/notifications/dnd" and method == "GET":
            return httpx.Response(200, json=self.dnd)
        if path == "/api/notifications/dnd" and method == "PUT":
            body = json.loads(request.content)
            self.puts.append(body)
            if not body["on"]:
                self.dnd = {"on": False, "since": None, "until": None, "break_critical": True}
            else:
                until = body.get("until")
                if body.get("minutes"):
                    until = (datetime.now(timezone.utc) + timedelta(minutes=body["minutes"])
                             ).strftime("%Y-%m-%dT%H:%M:%SZ")
                self.dnd = {"on": True, "since": "2026-10-01T03:00:00Z", "until": until,
                            "break_critical": True}
            return httpx.Response(200, json=self.dnd)
        return super().handle(request)


def _app(srv, token=FULL):
    return jav3.build_tui("http://h:1", token, transport=srv.transport())


def _status(app) -> str:
    return str(app.query_one("#status-right").render())


def _notes(app) -> list[str]:
    return [str(n.render()) for n in app.query("Note")]


async def test_dnd_turns_it_on_for_a_while_and_marks_the_status_row(cfg):
    srv = DndServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert "DND" not in _status(app)
        app.dispatch("/dnd 1h")
        assert await wait_for(lambda: srv.puts)
        assert srv.puts == [{"on": True, "minutes": 60}]
        assert await wait_for(lambda: "DND" in _status(app))
        assert "DND 1h" in _status(app)
        assert await wait_for(lambda: any("do not disturb is on until" in n for n in _notes(app)))
        app.dispatch("/dnd off")
        assert await wait_for(lambda: len(srv.puts) == 2)
        assert srv.puts[1] == {"on": False}
        assert await wait_for(lambda: "DND" not in _status(app))
        assert any("do not disturb is off" in n for n in _notes(app))


async def test_dnd_alone_is_on_until_turned_off_and_a_second_one_only_says_so(cfg):
    srv = DndServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/dnd")
        assert await wait_for(lambda: srv.puts == [{"on": True}])
        assert await wait_for(lambda: "DND" in _status(app))
        assert "DND" in _status(app) and "DND 1h" not in _status(app)   # no time left to show
        assert any("until you turn it off" in n for n in _notes(app))
        app.dispatch("/dnd")
        assert await wait_for(lambda: sum("is on until you turn it off" in n
                                          for n in _notes(app)) >= 2)
        assert srv.puts == [{"on": True}]                    # nothing sent the second time


async def test_dnd_tomorrow_is_the_next_8am_in_the_operators_zone(cfg):
    srv = DndServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/dnd tomorrow")
        assert await wait_for(lambda: srv.puts)
        until = datetime.strptime(srv.puts[0]["until"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)       # the zone is pinned to UTC
        assert (until.hour, until.minute) == (8, 0)
        assert now < until <= now + timedelta(hours=24)


async def test_dnd_takes_minutes_hours_and_days_and_refuses_the_rest(cfg):
    srv = DndServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        for arg in ("90m", "4h", "2d", "45"):
            app.dispatch(f"/dnd {arg}")
        assert await wait_for(lambda: len(srv.puts) == 4)
        assert [p["minutes"] for p in srv.puts] == [90, 240, 2880, 45]
        for bad in ("soon", "0", "-5m"):
            app.dispatch(f"/dnd {bad}")
        assert await wait_for(lambda: sum("is not a time" in n for n in _notes(app)) == 3)
        assert len(srv.puts) == 4


async def test_dnd_needs_full_access(cfg):
    srv = DndServer()
    app = _app(srv, token="device-token-abc")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/dnd 1h")
        assert await wait_for(lambda: any("needs full access" in n for n in _notes(app)))
        assert srv.puts == []


async def test_it_starts_marked_when_the_server_is_already_in_dnd(cfg):
    until = (datetime.now(timezone.utc) + timedelta(minutes=47)).strftime("%Y-%m-%dT%H:%M:%SZ")
    srv = DndServer({"on": True, "since": "2026-10-01T03:00:00Z", "until": until,
                     "break_critical": True})
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await wait_for(lambda: "DND" in _status(app))
        assert "DND 47m" in _status(app) or "DND 46m" in _status(app)


async def test_the_stream_flips_the_mark_and_the_summary_pings_once(cfg):
    srv = DndServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await wait_for(lambda: srv.streams_open)
        await pilot.pause(0.2)
        srv.say({"type": "dnd_changed", "on": True, "until": None, "break_critical": True})
        assert await wait_for(lambda: "DND" in _status(app))
        srv.say({"type": "dnd_changed", "on": False, "until": None, "break_critical": True})
        assert await wait_for(lambda: "DND" not in _status(app))
        # the summary the server sends when it ends is a ping: counted and listed
        srv.say({"type": "dnd_summary", "alerts": 3, "approvals": 1, "ping": True,
                 "summary": "While you were in do-not-disturb: 3 alerts, 1 approval waiting"})
        assert await wait_for(lambda: any("3 alerts, 1 approval" in t for _, _, t in app.notices))
        assert app.unread == 1


async def test_pings_stay_quiet_during_dnd_but_a_critical_break_through_and_the_summary_do_not(cfg):
    srv = DndServer({"on": True, "since": "2026-10-01T03:00:00Z", "until": None,
                     "break_critical": True})
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await wait_for(lambda: srv.streams_open and app.dnd)
        await pilot.pause(0.2)
        # the server held this ping back: ping False, so nothing in the sidebar
        srv.say({"type": "security_event", "id": 1, "kind": "unexpected_process",
                 "severity": "warn", "summary": "Unexpected process", "count": 1,
                 "repeat": False, "tier": "alert", "ping": False})
        # an agent finishing is the client's own notice: listed, silent
        srv.say({"type": "agent_run_done", "conversation_id": 44, "agent": "Coder", "ok": True,
                 "took": "1m", "summary": "done"}, topic="notices")
        assert await wait_for(lambda: any("Coder" in t for _, _, t in app.notices))
        assert app.unread == 0 and app.crit_unread == 0
        assert not any("unexpected_process" in t for _, _, t in app.notices)
        # a critical alert the server let through is a ping all the same
        srv.say({"type": "security_event", "id": 2, "kind": "egress_anomaly",
                 "severity": "critical", "summary": "volume spike", "count": 1,
                 "repeat": False, "tier": "critical", "ping": True})
        assert await wait_for(lambda: app.crit_unread == 1)
        assert app.unread == 1
        assert "✗ 1" in _status(app) and "DND" in _status(app)


async def test_the_same_agent_notice_counts_when_dnd_is_off(cfg):
    srv = DndServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await wait_for(lambda: srv.streams_open)
        await pilot.pause(0.2)
        srv.say({"type": "agent_run_done", "conversation_id": 44, "agent": "Coder", "ok": True,
                 "took": "1m", "summary": "done"}, topic="notices")
        assert await wait_for(lambda: app.unread == 1)
