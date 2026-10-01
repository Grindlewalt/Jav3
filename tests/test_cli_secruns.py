"""The terminal's /security Queue, one row per run (SB2, 2026-10-01): the cards from
GET /api/security/runs, the step and the agent's words (labelled untrusted) in the
detail, and plain keys for what to do: y the kind's own remedy, o the chat, x stop the
agent, X the whole run (asks), k kill a process, m mute the kind, a acknowledge, enter
opens the run to its events. A server with no /runs keeps the flat list."""
import json

import httpx
import pytest

from cli_fake import FakeServer, load_client

jav3 = load_client("jav3cli_secruns")
FULL = jav3.SESSION_PREFIX + "jwt"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path / "cfg" / "jav3"


STEP = {"id": 12123, "call_id": "call_9", "tool": "run_code", "match": "call",
        "command": "pkill -f 'serve.mjs'; nohup node scripts/serve.mjs", "args": "{}"}
DOING = {"conversation": {"id": 500, "kind": "chat", "summary": "Realistic Minecraft"},
         "step": STEP, "untrusted": True,
         "says": {"text": "Probe the screenshot tool: start serve.mjs in the box",
                  "source": "narration"}}


def ev(id, kind, summary, detail, **kw):
    return {"id": id, "kind": kind, "severity": kw.pop("severity", "warn"),
            "tier": kw.pop("tier", "alert"), "project_slug": "bg", "summary": summary,
            "detail": detail, "acknowledged": 0, "count": kw.pop("count", 1),
            "created_at": "2026-10-01 05:06:53", "last_seen": "2026-10-01 05:06:53",
            "conversation_id": 500, "doing": kw.pop("doing", DOING), **kw}


PROC = ev(9, "unexpected_process", "Unexpected process in box p-bg: /usr/bin/node",
          {"box_id": "p-bg", "pid": 812, "exe": "/usr/bin/node", "cmd": "node serve.mjs",
           "unit": "", "start_ticks": 5})
PROC2 = ev(8, "unexpected_process", "Unexpected process in box p-bg: /opt/crashpad",
           {"box_id": "p-bg", "pid": 9, "exe": "/opt/crashpad", "cmd": "crashpad", "unit": ""})
WRITE = ev(7, "write_flag", "write flag: assertion_removed in tests/world.test.mjs",
           {"path": "tests/world.test.mjs", "trigger": "assertion_removed"})
CUT = ev(6, "egress_anomaly", "high-entropy host a8f3.net",
         {"host": "a8f3.net"}, severity="critical", tier="critical")
REPORT = ev(5, "harness_fault", "Harness fault reported: screenshot: blank", {"tool": "screenshot"},
            severity="info", tier="record")

RUN = {"key": "run:500", "group": "run", "title": 'bg · chat 500 "Realistic Minecraft"',
       "project": "bg", "running": True, "tier": "alert", "severity": "warn", "newest_id": 9,
       "counts": {"need": 3, "reports": 1, "filtered": 47, "record": 0},
       "kinds": [{"kind": "unexpected_process", "n": 2, "count": 2, "severity": "warn",
                  "tier": "alert", "subjects": ["node", "crashpad"], "ids": [9, 8]},
                 {"kind": "write_flag", "n": 1, "count": 1, "severity": "warn",
                  "tier": "alert", "subjects": ["tests/world.test.mjs"], "ids": [7]}],
       "report_kinds": [{"kind": "harness_fault", "n": 1, "count": 1, "severity": "info",
                         "tier": "record", "subjects": [], "ids": [5]}],
       "last_at": "2026-10-01 05:31:00", "events": [PROC, PROC2, WRITE, REPORT]}
BOX = {"key": "box:p-other", "group": "box", "title": "box p-other", "project": None,
       "running": None, "tier": "critical", "severity": "critical", "newest_id": 6,
       "counts": {"need": 1, "reports": 0, "filtered": 0, "record": 0},
       "kinds": [{"kind": "egress_anomaly", "n": 1, "count": 1, "severity": "critical",
                  "tier": "critical", "subjects": ["a8f3.net"], "ids": [6]}],
       "report_kinds": [], "last_at": "2026-10-01 04:00:00",
       "events": [{**CUT, "doing": None, "conversation_id": None}]}


class RunsServer(FakeServer):
    """The chat server plus the Queue's routes. `runs` is None for a server that has no cards."""

    def __init__(self, runs=(RUN, BOX)) -> None:
        super().__init__(full=True)
        self.runs = list(runs) if runs is not None else None
        self.writes: list[tuple[str, str, dict | None]] = []
        self.refuse: dict[str, str] = {}               # path suffix -> a 409 detail
        self.stop_dry = {"agents": [500, 501, 502], "plans": [], "needs_confirm": False,
                         "message": "3 agents running would stop"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        if path == "/api/security/runs" and method == "GET":
            if self.runs is None:
                return httpx.Response(404, json={"detail": "nope"})
            return httpx.Response(200, json={"runs": self.runs, "totals": {}})
        if path == "/api/security/events" and method == "GET":
            return httpx.Response(200, json={"events": [PROC]})
        if path.startswith("/api/security/") or path == "/api/notifications/settings":
            if method != "GET":
                self.writes.append((method, path, body))
                for suffix, why in self.refuse.items():
                    if path.endswith(suffix):
                        return httpx.Response(409, json={"detail": why})
                if path.endswith("/stop") and body and body.get("dry_run"):
                    return httpx.Response(200, json=self.stop_dry)
                return httpx.Response(200, json={"ok": True, "message": "done", "acknowledged": 1})
        for fixed in ("/api/egress/pending", "/api/packages"):
            if path == fixed:
                return httpx.Response(200, json={"pending": [], "packages": []})
        if path == "/api/services":
            return httpx.Response(200, json={"services": []})
        if path.endswith("/git/requests"):
            return httpx.Response(200, json={"requests": []})
        return super().handle(request)


def _app(srv):
    return jav3.build_tui("http://h:1", FULL, transport=srv.transport())


async def _until(pilot, cond, tries=80):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


def _text(widget) -> str:
    return str(widget.render())


def _rows(scr):
    return [str(r.render()) for r in scr.query("SecRow")]


async def _open(pilot, app):
    app.dispatch("/security")
    assert await _until(pilot, lambda: type(app.top).__name__ == "SecurityPage")
    scr = app.top
    assert await _until(pilot, lambda: scr.loaded["queue"] and len(scr.entries["queue"]) > 0)
    await pilot.pause(0.1)
    return scr


async def _confirm(pilot, app, yes=True):
    assert await _until(pilot, lambda: type(app.top).__name__ == "Confirm")
    dlg = app.top
    await pilot.press("y" if yes else "n")
    assert await _until(pilot, lambda: type(app.top).__name__ != "Confirm")
    return dlg


async def test_the_queue_is_one_row_per_run_with_the_counts(cfg):
    srv = RunsServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        scr = await _open(pilot, app)
        rows = _rows(scr)
        assert len(rows) == 2                                   # two runs, not eight events
        assert 'chat 500 "Realistic Minecraft"' in rows[0] and "running" in rows[0]
        assert "3 need you · 47 filtered · 1 agent report" in rows[0]
        assert "box p-other" in rows[1]
        assert "2 alerts in 2 runs" in _text(scr.query_one("#sec-sub")) or \
            "4 alerts in 2 runs" in _text(scr.query_one("#sec-sub"))
        assert "Queue 2" in _text(scr.query_one("#sec-tab-queue"))


async def test_the_detail_names_the_step_and_labels_the_agents_words(cfg):
    srv = RunsServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        scr = await _open(pilot, app)
        d = _text(scr.query_one("#sec-detail"))
        assert "the agent's words: untrusted" in d
        assert "chat 500 → run_code #12123: pkill -f 'serve.mjs'" in d
        assert "Probe the screenshot tool" in d
        keys = _text(scr.query_one("#sec-foot"))
        for k in ("y allow node", "o open chat", "x stop agent", "X stop run", "k kill",
                  "m mute kind", "a acknowledge group", "enter events"):
            assert k in keys, k


async def test_enter_opens_a_run_to_its_events_and_keys_follow_the_event(cfg):
    srv = RunsServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        scr = await _open(pilot, app)
        await pilot.press("enter")
        assert await _until(pilot, lambda: len(_rows(scr)) == 6)
        assert "Queue 2" in _text(scr.query_one("#sec-tab-queue"))      # events are not more cards
        rows = _rows(scr)
        assert "unexpected_process" in rows[1] and "node" in rows[1] and "run_code #12123" in rows[1]
        assert "write_flag" in rows[3] and "tests/world.test.mjs" in rows[3]
        await pilot.press("down", "down", "down")                       # onto the write flag
        keys = _text(scr.query_one("#sec-foot"))
        assert "y revert file" in keys and "k kill" not in keys
        assert "a acknowledge" in keys and "acknowledge group" not in keys
        await pilot.press("enter")                                      # on an event: nothing
        await pilot.press("up", "up", "up")
        await pilot.press("enter")                                      # the card closes
        assert await _until(pilot, lambda: len(_rows(scr)) == 2)


async def test_y_allows_the_program_after_a_confirm(cfg):
    srv = RunsServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        scr = await _open(pilot, app)
        await pilot.press("y")
        dlg = await _confirm(pilot, app, yes=False)
        assert "/usr/bin/node" in dlg.question and srv.writes == []
        await pilot.press("y")
        await _confirm(pilot, app)
        assert await _until(pilot, lambda: srv.writes)
        assert srv.writes[0] == ("POST", "/api/security/events/9/baseline", {"scope": "program"})
        assert scr is app.top or await _until(pilot, lambda: app.top is scr)


async def test_y_reverts_a_flagged_file_and_a_refusal_is_shown(cfg):
    srv = RunsServer()
    srv.refuse["/revert"] = "tests/world.test.mjs has changed since this alert: not touching it."
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        scr = await _open(pilot, app)
        await pilot.press("enter", "down", "down", "down")
        await pilot.press("y")
        dlg = await _confirm(pilot, app)
        assert "tests/world.test.mjs" in dlg.question and "git HEAD" in dlg.detail
        assert await _until(pilot, lambda: srv.writes)
        assert srv.writes[0][1] == "/api/security/events/7/revert"
        assert await _until(pilot, lambda: "has changed since this alert" in
                            _text(scr.query_one("#sec-sub")))


async def test_y_uncuts_a_host(cfg):
    srv = RunsServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        scr = await _open(pilot, app)
        await pilot.press("down")                                      # the cut's card
        assert "y un-cut host" in _text(scr.query_one("#sec-foot"))
        assert "m mute" not in _text(scr.query_one("#sec-foot"))       # critical: never muted
        await pilot.press("y")
        await _confirm(pilot, app)
        assert await _until(pilot, lambda: srv.writes)
        assert srv.writes[0][1] == "/api/security/events/6/uncut"


async def test_x_stops_one_agent_and_X_asks_before_stopping_the_run(cfg):
    srv = RunsServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        scr = await _open(pilot, app)
        await pilot.press("x")
        await _confirm(pilot, app)
        assert await _until(pilot, lambda: srv.writes)
        assert srv.writes[-1] == ("POST", "/api/security/events/9/stop", {"scope": "agent"})
        srv.writes.clear()
        assert await _until(pilot, lambda: app.top is scr)
        await pilot.press("X")
        dlg = await _confirm(pilot, app, yes=False)
        assert "3 agents" in dlg.question
        assert [w[2] for w in srv.writes] == [{"scope": "run", "dry_run": True}]   # nothing stopped
        await pilot.press("X")
        await _confirm(pilot, app)
        assert await _until(pilot, lambda: srv.writes[-1][2] == {"scope": "run", "confirm": True})


async def test_k_kills_a_process_and_is_still_up_elsewhere(cfg):
    srv = RunsServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        scr = await _open(pilot, app)
        await pilot.press("k")
        dlg = await _confirm(pilot, app)
        assert "pid 812" in dlg.question and "reused" in dlg.detail
        assert await _until(pilot, lambda: srv.writes)
        assert srv.writes[0][1] == "/api/security/events/9/kill"
        # on a card that names no process, k moves up like it always did
        assert await _until(pilot, lambda: app.top is scr)
        await pilot.press("down")
        assert scr.sel["queue"] == "rbox:p-other"
        await pilot.press("k")
        assert scr.sel["queue"] == "rrun:500"
        assert len(srv.writes) == 1


async def test_m_mutes_the_kind_through_the_pick_and_a_critical_kind_cannot(cfg):
    srv = RunsServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        scr = await _open(pilot, app)
        await pilot.press("m")
        assert await _until(pilot, lambda: type(app.top).__name__ == "Picker")
        await pilot.press("enter")                                      # "record" is preselected
        assert await _until(pilot, lambda: srv.writes)
        assert srv.writes[0] == ("PUT", "/api/notifications/settings",
                                 {"kinds": {"unexpected_process": "record"}})
        assert await _until(pilot, lambda: app.top is scr)
        await pilot.press("down")
        await pilot.press("m")
        assert await _until(pilot, lambda: "cannot be muted" in _text(scr.query_one("#sec-sub")))
        assert len(srv.writes) == 1


async def test_a_acknowledges_the_group_and_on_an_event_that_event(cfg):
    srv = RunsServer()
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        scr = await _open(pilot, app)
        await pilot.press("a")
        dlg = await _confirm(pilot, app)
        assert "everything in" in dlg.question and "3 alerts" in dlg.detail
        assert await _until(pilot, lambda: srv.writes)
        assert srv.writes[0][:2] == ("POST", "/api/security/runs/run:500/ack")
        srv.writes.clear()
        assert await _until(pilot, lambda: app.top is scr)
        await pilot.press("enter", "down")
        await pilot.press("a")
        await _confirm(pilot, app)
        assert await _until(pilot, lambda: srv.writes)
        assert srv.writes[0][1] == "/api/security/events/9/ack"


async def test_o_opens_the_runs_chat(cfg):
    srv = RunsServer()
    srv.conv_messages[500] = {"messages": [{"id": 1, "role": "user", "content": "build it",
                                            "created_at": "t"}], "running": False,
                              "pending_activity": [], "agent_slug": None}
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await _open(pilot, app)
        await pilot.press("o")
        assert await _until(pilot, lambda: ("GET", "/api/conversations/500/messages")
                            in srv.calls)
        assert await _until(pilot, lambda: app.cid == 500)


async def test_a_server_without_runs_keeps_the_flat_alert_list(cfg):
    srv = RunsServer(runs=None)
    app = _app(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        scr = await _open(pilot, app)
        rows = _rows(scr)
        assert len(rows) == 1 and "Unexpected process in box p-bg" in rows[0]
        assert scr.entries["queue"][0]["type"] == "alert"
        await pilot.press("y")                      # an alert is not an approval, as before
        assert await _until(pilot, lambda: "not an approval" in _text(scr.query_one("#sec-sub")))
