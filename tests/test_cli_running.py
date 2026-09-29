"""The terminal client while a turn runs (clients/jav3cli/jav3): one line per
tool call, what came back in a few words, the steady status line and the ▣
footer, rows that stop when the turn fails, picking and opening a row, and the
"↓ n new" hint when the operator has scrolled up."""
import asyncio
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

CLI = Path(__file__).resolve().parent.parent / "clients" / "jav3cli" / "jav3"


def _load():
    loader = importlib.machinery.SourceFileLoader("jav3cli_running", str(CLI))
    spec = importlib.util.spec_from_loader("jav3cli_running", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


jav3 = _load()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path / "cfg" / "jav3"


# --- pure helpers ----------------------------------------------------------------------

def test_elapsed_cost_and_plural():
    assert jav3._elapsed(0.42, precise=True) == "0.4s"
    assert jav3._elapsed(0.42) == "0s"
    assert jav3._elapsed(59.9) == "59s"
    assert jav3._elapsed(133) == "2m 13s"
    assert jav3._elapsed(3 * 3600 + 125) == "3h 02m"
    assert jav3._elapsed(-1) == "0s"
    assert jav3._fmt_cost(None) == "$0.00" and jav3._fmt_cost(0) == "$0.00"
    assert jav3._fmt_cost(0.0234) == "$0.02"
    assert jav3._fmt_cost(0.0042) == "$0.004"
    assert jav3._fmt_cost(0.00001) == "$0.001"
    assert jav3._plural(1, "tool") == "1 tool" and jav3._plural(3, "tool") == "3 tools"
    assert jav3._plural(2, "match", "matches") == "2 matches"


def test_fit_parts_drops_the_least_important_first():
    parts = [("working 5s", "W", 0), ("14 tools", "T", 1), ("2 agents", "A", 3),
             ("$0.02", "C", 2), ("esc interrupt", "E", 4)]
    assert jav3.fit_parts(parts, 200) == "W · T · A · C · E"
    # 60 fits all; 40 loses esc, then agents; the first part always stays
    assert jav3.fit_parts(parts, 45) == "W · T · A · C"
    assert jav3.fit_parts(parts, 30) == "W · T · C"
    assert jav3.fit_parts(parts, 3) == "W"
    assert jav3.fit_parts(parts[:2], 200, markup_sep="|") == "W|T"


def test_cmd_line_drops_the_habitual_cd():
    assert jav3._cmd_line('cd "$(pwd)"; node -v; npm test') == "node -v; npm test"
    assert jav3._cmd_line("cd /opt/p && make") == "make"
    assert jav3._cmd_line("cd /opt/p 2>/dev/null || cd \"$(pwd)\"\nfor f in *; do x; done") \
        == "for f in *; do x; done"
    assert jav3._cmd_line("cd /opt/p\ngrep -n x a.js\nsed -n 1p b") == "grep -n x a.js …"
    assert jav3._cmd_line("python3 - <<'PY'\nprint(1)\nPY") == "python3 - <<'PY' …"
    assert jav3._cmd_line("cd /somewhere") == "cd /somewhere"
    assert jav3._cmd_line("sleep 5; cd x; ls") == "sleep 5; cd x; ls"
    assert jav3._cmd_line("") == "" and jav3._cmd_line(None) == ""


def test_tool_titles_the_running_view_reads():
    assert jav3.tool_title("run_code", {"command": 'cd "$(pwd)"; npm run test:unit'}) == \
        ("$", "npm run test:unit")
    assert jav3.tool_title("run_code", {}) == ("$", "run_code")     # attached late: no args
    icon, title = jav3.tool_title("read_and_summarize",
                                  {"urls": ["https://a.example/x", "https://b.example/y"]})
    assert title == "Read https://a.example/x (+1 more)" and "[" not in title
    assert jav3.tool_title("ask_user", {"questions": [{"question": "Which port?"}]}) == \
        ("?", "Ask you Which port?")
    assert jav3.tool_title("send_message", {"to": "item:i2", "message": "hold off"}) == \
        ("✉", "Message item:i2  hold off")
    assert jav3.tool_title("local_shell", {"command": "cd ~/x && ls"}) == ("$", "ls  (local)")


def test_result_hints_say_what_came_back():
    h = jav3.result_hint
    assert h("read_file", {}, True, "(lines 1-230 of 656 — a.js)\nx") == "lines 1-230 of 656"
    assert h("read_file", {}, True, "a\nb\nc") == "3 lines"
    assert h("search_codebase", {}, True,
             "a.js (20 matches)\n  1: x\nb.js (1 match)\n  2: y") == "21 matches in 2 files"
    assert h("search_codebase", {}, True, "no matches for 'x'. Try …") == "no matches"
    assert h("list_files", {}, True, "project: p\na.md  [1B]\nb.md  [2B]") == "2 entries"
    assert h("web_search", {}, True, "search: q\n\n1. A\n   u\n2. B\n   v") == "2 results"
    assert h("run_code", {}, True, "exit 0 · 21.21s\n--- stdout ---\nok 1\nok 2\n\nok 3") == \
        "exit 0 · 3 lines"
    assert h("run_code", {}, True, "exit -9 · 300.03s · KILLED after 300s timeout\n"
             "--- stdout ---\na") == "exit -9 · KILLED after 300s timeout · 1 line"
    assert h("run_code", {}, True, "exit 0 · 0.1s") == "exit 0 · no output"
    assert h("edit_file", {"find": "a\nb", "replace": "a\nc\nd"}, True, "edited") == "+2 −1"
    assert h("write_file", {"content": "x"}, True, "wrote a (1 chars)") == ""
    assert h("todo_update", {}, True, "0. [x] one\n1. [ ] two") == "1/2 done"
    assert h("read_file", {}, False, "error: no such file 'x'\nmore") == "no such file 'x'"
    assert h("spawn_agent", {}, True, "Agent #5 finished\nsummary") == \
        "Agent #5 finished  (+1 lines)"
    assert h("mystery", {}, True, "") == ""


def test_tool_body_caps_and_shows_a_long_command_first():
    out = "\n".join(map(str, range(100)))
    body, more = jav3.tool_body("run_code", {"command": "seq 100"}, True, out, True, cap=40)
    assert more == 60 and body.count("\n") == 39 and "$ seq" not in body
    cmd = "cd x\nfor i in 1 2; do\n  echo $i\ndone"
    body, more = jav3.tool_body("run_code", {"command": cmd}, True, "1\n2", True)
    assert body.splitlines()[0] == "[$text-muted]$ cd x[/]" and body.endswith("2")
    # a running call has only its command to show; a short one, nothing
    assert len(jav3.tool_body("run_code", {"command": cmd}, None, None, True)[0]
               .splitlines()) == 4
    assert jav3.tool_body("run_code", {"command": "ls"}, None, None, True) == (None, 0)
    # an open generic row: the long argument the title cut, then the result
    body, _ = jav3.tool_body("spawn_agent", {"agent": "coder", "task": "x" * 90}, True,
                             "done", True)
    assert body.startswith("[$text-muted]task:[/] xxx") and body.endswith("done[/]")
    # the collapsed forms are unchanged
    assert jav3.tool_body("read_file", {"path": "a"}, True, "text", False) == (None, 0)


def test_turn_state_counts_and_freezes():
    t = jav3.TurnState()
    t.started -= 75
    for name in ("read_file", "spawn_agent", "research", "edit_file"):
        t.count(name)
    t.plans = 1
    assert t.stats()[1:] == ["4 tools", "2 agents", "1 plan"]
    t.cost = 0.0234
    assert t.stats()[0] == "1m 15s" and t.stats()[-1] == "$0.02"
    t.ended = t.started + 30
    assert t.stats()[0] == "30s"


# --- the app, headless -----------------------------------------------------------------

class Feed:
    """A chat stream the test writes into, event by event."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.posted: list = []

    def send(self, *evs):
        for ev in evs:
            self.q.put_nowait(ev)

    async def body(self):
        while True:
            ev = await self.q.get()
            if ev is None:
                return
            yield f"data: {json.dumps(ev)}\n\n".encode()

    def transport(self):
        def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p == "/api/devices/whoami":
                return httpx.Response(200, json={"username": "device:test"})
            if p == "/api/chat/options":
                return httpx.Response(200, json={"default": "deepseek/deepseek-flash",
                                                 "models": [], "projects": [], "agents": []})
            if p == "/api/conversations":
                return httpx.Response(200, json={"conversations": []})
            if p.endswith("/info"):
                return httpx.Response(200, json={"title": "t", "cost_usd": 0.0123})
            if p.endswith("/message"):
                self.posted.append(json.loads(request.content))
                return httpx.Response(200, json={"queued": True})
            if p == "/api/chat":
                return httpx.Response(200, content=self.body(),
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(404, json={"detail": "nope"})
        return httpx.MockTransport(handler)


async def _until(pilot, cond, n=80):
    for _ in range(n):
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


def _plain(widget) -> str:
    return str(widget.render())


async def _start(pilot, app, feed, text="go"):
    await pilot.pause(0.3)
    app.editor.text = text
    await pilot.press("enter")
    feed.send({"type": "start", "conversation_id": 9, "model": "deepseek/deepseek-flash"})
    assert await _until(pilot, lambda: app.turn is not None and app.turn.cid == 9)


async def test_parallel_calls_are_one_line_each_and_keep_their_own_results(cfg):
    pytest.importorskip("textual")
    feed = Feed()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=feed.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await _start(pilot, app, feed)
        feed.send(*[{"type": "tool", "id": f"c{i}", "name": "read_file",
                     "args": {"path": f"f{i}.py"}} for i in range(3)])
        assert await _until(pilot, lambda: len(app.query("ToolView")) == 3)
        # the status row is redrawn on the 0.1 s tick
        assert await _until(pilot, lambda: "3 tools" in _plain(app.query_one("#status-left")))
        assert "working" in _plain(app.query_one("#status-left"))
        # results come back out of order: each lands on its own row
        for i, n in ((2, 5), (0, 1), (1, 3)):
            feed.send({"type": "tool_result", "id": f"c{i}", "name": "read_file", "ok": True,
                       "result": "\n".join("x" * n)})
        assert await _until(pilot, lambda: all(tv.done for tv in app.query("ToolView")))
        await pilot.pause(0.2)
        rows = list(app.query("ToolView"))
        heads = [_plain(tv.query_one("#head")) for tv in rows]
        assert [h.split("  ")[-1] for h in heads] == ["1 line", "3 lines", "5 lines"]
        assert all(tv.size.height == 1 for tv in rows)          # collapsed: one line
        assert all(_plain(tv.query_one("#dur")).endswith("s") for tv in rows)
        feed.send({"type": "token", "text": "All read."},
                  {"type": "final", "content": "All read.", "conversation_id": 9})
        assert await _until(pilot, lambda: not app.busy)
        foot = _plain(list(app.query("Footer"))[-1])
        assert foot.startswith("▣ Jav3 · deepseek-flash · ") and "3 tools" in foot
        assert "$0.01" in foot or "$0.00" in foot


async def test_a_failed_turn_stops_its_rows_and_hands_messages_back(cfg):
    pytest.importorskip("textual")
    feed = Feed()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=feed.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await _start(pilot, app, feed)
        feed.send({"type": "tool", "id": "c1", "name": "run_code",
                   "args": {"command": "npm test"}})
        assert await _until(pilot, lambda: len(app.query("ToolView")) == 1)
        tv = app.query_one("ToolView")
        assert tv in app.live_tools                # its timer is ticking
        feed.send({"type": "error", "message": "the VM stopped responding",
                   "undelivered": ["and check the fog"]})
        assert await _until(pilot, lambda: not app.busy)
        await pilot.pause(0.1)
        assert tv.interrupted and tv not in app.live_tools
        assert "the turn failed" in _plain(tv.query_one("#head"))
        assert "failed" in _plain(list(app.query("Footer"))[-1])
        assert app.editor.text == "and check the fog"     # back to the prompt, not lost


async def test_calls_without_ids_are_matched_by_name_in_order(cfg):
    pytest.importorskip("textual")
    feed = Feed()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=feed.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await _start(pilot, app, feed)
        feed.send({"type": "tool", "name": "read_file", "args": {"path": "a"}},
                  {"type": "tool", "name": "read_file", "args": {"path": "b"}},
                  {"type": "tool_result", "name": "read_file", "ok": True, "result": "1"},
                  {"type": "tool_result", "name": "read_file", "ok": True, "result": "1\n2"})
        assert await _until(pilot, lambda: all(tv.done for tv in app.query("ToolView"))
                            and len(app.query("ToolView")) == 2)
        a, b = app.query("ToolView")
        assert (a.args["path"], a.result) == ("a", "1") and (b.args["path"], b.result) == \
            ("b", "1\n2")
        feed.send({"type": "final", "content": "", "conversation_id": 9})
        assert await _until(pilot, lambda: not app.busy)


async def test_tab_picks_a_row_and_enter_opens_it(cfg):
    pytest.importorskip("textual")
    feed = Feed()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=feed.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await _start(pilot, app, feed)
        for i in range(3):
            feed.send({"type": "tool", "id": f"c{i}", "name": "run_code",
                       "args": {"command": f"echo {i}"}},
                      {"type": "tool_result", "id": f"c{i}", "name": "run_code", "ok": True,
                       "result": f"exit 0 · 0.1s\n--- stdout ---\nout {i}"})
        feed.send({"type": "final", "content": "done", "conversation_id": 9})
        assert await _until(pilot, lambda: not app.busy)
        rows = list(app.query("ToolView"))
        assert not any(tv.expanded for tv in rows)
        await pilot.press("tab")
        await pilot.pause(0.1)
        log = app.log_view
        assert log.has_focus and log.picked is rows[-1] and rows[-1].has_class("-picked")
        assert "pick" in _plain(app.query_one("#status-left"))
        await pilot.press("up", "enter")
        await pilot.pause(0.1)
        assert log.picked is rows[1] and rows[1].expanded and not rows[2].expanded
        assert "out 1" in _plain(rows[1].query_one("#body"))
        await pilot.press("left")                  # closes it again
        await pilot.pause(0.05)
        assert not rows[1].expanded
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert app.editor.has_focus and not rows[1].has_class("-picked")
        await pilot.press("tab", "h", "i")         # typing leaves the rows, key and all
        await pilot.pause(0.05)
        assert app.editor.has_focus and app.editor.text == "hi"


async def test_new_below_hint_while_scrolled_up(cfg):
    pytest.importorskip("textual")
    feed = Feed()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=feed.transport())
    async with app.run_test(size=(100, 20)) as pilot:
        await _start(pilot, app, feed)
        for i in range(30):
            feed.send({"type": "tool", "id": f"a{i}", "name": "read_file",
                       "args": {"path": f"f{i}"}},
                      {"type": "tool_result", "id": f"a{i}", "name": "read_file", "ok": True,
                       "result": "x"})
        assert await _until(pilot, lambda: len(app.query("ToolView")) == 30)
        await pilot.pause(0.2)
        hint = app.query_one("#new-hint")
        assert not hint.display
        await pilot.press("pageup")                # the operator scrolls up to read
        await pilot.pause(0.2)
        feed.send(*[{"type": "tool", "id": f"b{i}", "name": "read_file",
                     "args": {"path": f"g{i}"}} for i in range(4)])
        assert await _until(pilot, lambda: len(app.query("ToolView")) == 34)
        assert await _until(pilot, lambda: hint.display)
        assert "4 new below" in _plain(hint)
        await pilot.click("#new-hint")             # jumps back down and follows again
        assert await _until(pilot, lambda: not hint.display)
        feed.send({"type": "final", "content": "done", "conversation_id": 9})
        assert await _until(pilot, lambda: not app.busy)
