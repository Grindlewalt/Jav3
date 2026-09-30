"""Terminal client polish: /help as a list (TUI-05), reply-ready toasts (TUI-07),
/sessions rows (TUI-12), the picker hint (TUI-13), code lines (TUI-17), the hint bar
(TUI-19), idle redraws (TUI-20), the home hints (TUI-21), 256-colour rows and the
/security labels."""
import io

import pytest

from cli_fake import FakeServer, finish, load_client, open_chat, top, wait_for

jav3 = load_client("jav3cli_polish")


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / "cfg" / "jav3"


def render(view, width: int) -> list[str]:
    from rich.console import Console
    con = Console(width=width, file=io.StringIO(), force_terminal=False)
    return [ln.rstrip() for ln in
            "".join(seg.text for seg in con.render(view)).split("\n")]


def make_app(srv=None, **kw):
    srv = srv or FakeServer()
    return srv, jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport(), **kw)


# TUI-05

def test_help_is_two_lines_per_command_and_keeps_angle_brackets():
    keys = [("shift+tab", "permissions: yolo, auto or ask")]
    cmds = [("/themes [name|create|export [path]|import <path>]", "list, apply, make", ""),
            ("/help", "commands and keys", "ctrl+x h")]
    view, plain = jav3.help_view(keys, cmds)
    lines = render(view, 76)
    assert lines.index("Keys") < lines.index("Commands")
    assert "/themes [name|create|export [path]|import <path>]" in lines
    assert "  list, apply, make" in lines
    help_line = next(ln for ln in lines if ln.startswith("/help"))
    assert help_line.endswith("ctrl+x h") and len(help_line) == 76
    assert "  commands and keys" in lines
    assert "shift+tab" in plain and "<path>" in plain


async def test_help_dialog_uses_most_of_the_terminal():
    srv, app = make_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/help")
        assert await wait_for(lambda: top(app) == "Help")
        await pilot.pause(0.2)
        assert app.screen.query_one("#dialog").size.width >= 100
        assert "/theme create" in app.screen.text and "ctrl+x then a key" in app.screen.text
        # keys first, the commands after them
        assert app.screen.text.index("Keys") < app.screen.text.index("Commands")


# TUI-07

def ready(app) -> list[str]:
    return [t for _, _, t in app.notices if "reply ready" in t]


async def test_no_reply_ready_toast_for_the_chat_you_are_looking_at():
    srv, app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        await finish(srv, app)
        await pilot.pause(0.2)
        assert ready(app) == [] and app.unread == 0


async def test_reply_ready_when_the_window_is_elsewhere():
    srv, app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        app.app_focus = False
        await finish(srv, app)
        assert await wait_for(lambda: ready(app))
        assert app.unread == 1


async def test_watching_means_this_chat_on_the_main_screen():
    srv, app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        assert await wait_for(lambda: app.cid == 4)
        assert app._watching(4)
        assert not app._watching(99)               # a reply in another chat
        await finish(srv, app)
        from textual.screen import Screen
        await app.push_screen(Screen())            # like the agents screen
        assert not app._watching(4)
        await app.pop_screen()
        assert app._watching(4)


# TUI-12

def test_session_when_is_local_and_says_today_or_yesterday(monkeypatch):
    import time
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        now = 1790000000.0                     # 2026-09-21 13:33 UTC = 06:33 PDT
        assert jav3.session_when("2026-09-21 11:19:05", now) == "today 04:19"
        assert jav3.session_when("2026-09-21T02:10", now) == "yesterday 19:10"
        assert jav3.session_when("2026-09-12 20:00:00", now) == "09-12 13:00"
        assert jav3.session_when(None, now) == "" and jav3.session_when("soon", now) == ""
    finally:
        monkeypatch.undo()
        time.tzset()


class SessionsServer(FakeServer):
    def __init__(self, convs):
        super().__init__()
        self.convs = convs

    def handle(self, request):
        import httpx
        if request.url.path == "/api/conversations":
            return httpx.Response(200, json={"conversations": self.convs})
        return super().handle(request)


async def test_sessions_rows_keep_the_meta_on_one_line():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    long_title = "Explain why the sky is blue in two long sentences, then compare it to sunsets"
    srv = SessionsServer([{"id": 7, "summary": long_title, "started_at": now,
                           "project_slug": "benchmark-game", "running": True}])
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/sessions")
        assert await wait_for(lambda: top(app) == "Picker")
        await pilot.pause(0.2)
        (value, label, meta), = app.screen.rows
        assert value == "7" and meta.startswith("today ") and "⌂ benchmark-game" in meta
        assert label.endswith("…")
        ol = app.screen.query_one("#choices")
        assert ol.virtual_size.height == 1              # one line: nothing wrapped


# TUI-13

def model_rows():
    rows = [("default", "Server default", "")]
    for p in ("deepseek", "moonshot"):
        rows.append((None, p, ""))
        rows += [(f"{p}/m{i}", f"Model {i}", "") for i in range(12)]
    rows += [(None, "LMStudio  (provider off)", ""), ("lm/a", "Llama A", "off"),
             ("lm/b", "Llama B", "off")]
    return rows


async def test_picker_hint_stays_visible_on_24_rows():
    srv, app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)

        async def noop(v):
            return None
        app.run_worker(app.pick("Models", model_rows(), None, hint="enter pins the model",
                                actions={"space": ("on/off", noop),
                                         "d": ("make default", noop)}))
        assert await wait_for(lambda: top(app) == "Picker")
        await pilot.pause(0.2)
        dlg, hint = app.screen.query_one("#dialog"), app.screen.query_one("#dialog-hint")
        assert dlg.region.bottom <= 24
        assert hint.region.bottom <= dlg.region.bottom - 1      # inside the padding
        await pilot.press("t")                                  # the filter box takes rows too
        await pilot.pause(0.2)
        assert hint.region.bottom <= dlg.region.bottom - 1


async def test_hidden_rows_wait_for_a_filter_and_a_line_says_how_many():
    srv, app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        hidden = {"lm/a", "lm/b", "moonshot/m3"}
        app.run_worker(app.pick("Models", model_rows(), None, hidden=hidden))
        assert await wait_for(lambda: top(app) == "Picker")
        await pilot.pause(0.2)
        ol = app.screen.query_one("#choices")
        ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]
        assert "lm/a" not in ids and "moonshot/m3" not in ids and "moonshot/m4" in ids
        prompts = [ol.get_option_at_index(i).prompt for i in range(ol.option_count)]
        assert "LMStudio" not in "".join(str(p) for p in prompts)      # no empty heading
        assert "3 more hidden" in str(prompts[-1])
        await pilot.press("l", "l", "a")
        await pilot.pause(0.2)
        ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]
        assert ids[:2] == ["lm/a", "lm/b"]


# TUI-17

async def test_a_long_code_line_wraps_instead_of_scrolling_out_of_sight():
    long = ("# Jupiter is the largest planet in the solar system; its mass exceeds that of "
            "all the other planets combined, more than two and a half times over")
    srv, app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await open_chat(pilot, app, srv)
        srv.feed.put({"type": "final", "conversation_id": 4,
                      "content": f"```python\n{long}\nx = 1\n```\n"})
        srv.feed.close()
        assert await wait_for(lambda: not app.busy)
        assert await wait_for(lambda: list(app.query("MarkdownFence")))
        await pilot.pause(0.3)
        fence = list(app.query("MarkdownFence"))[0]
        assert fence.virtual_size.width <= fence.size.width      # nothing off to the side
        assert fence.size.height >= 5                            # the long line took 2+ rows


# security rows and the agents screen at 80 columns / 256 colours

def _plain(markup: str) -> str:
    import re
    return re.sub(r"\[[^\]]*\]", "", markup)


def test_calls_row_has_a_compact_form_that_keeps_cache_and_cost():
    e = {"type": "call", "ts": "2026-09-27 14:03:11", "raw": {
        "op_id": "7f3a9c2e51b64d08aa", "project_slug": "benchmark-game",
        "conversation_id": 4242, "model": "deepseek/deepseek-flash",
        "input_tokens": 12400, "output_tokens": 812, "cache_hit": 9100, "cost_usd": 0.0021}}
    full = _plain(jav3.calls_row(e))
    compact = _plain(jav3.calls_row(e, compact=True))
    assert "op 7f3a" in full and "⌂ benchmark-game" in full and len(full) > 76
    assert "op " not in compact and "⌂" not in compact and "#4242" in compact
    assert len(compact) <= 76 and compact.endswith("cache 9.1k $0.0021")
    assert jav3.calls_row(e) == jav3.calls_row(e, compact=False)      # the spec'd string


def test_network_verdicts_are_whole_words():
    assert jav3.verdict_label("pending") == "PENDING"
    assert jav3.verdict_label("allow") == "ALLOW" and jav3.verdict_label("cut") == "CUT"
    assert jav3.verdict_label("auto_allow") == "A·ALLOW"
    assert jav3.verdict_label("auto_deny") == "A·DENY"
    assert jav3.verdict_label(None) == "?"


def test_current_agent_row_uses_solid_colours_for_256_colour_terminals():
    from cli_fake import CLI
    src = CLI.read_text()
    rule = next(ln for ln in src.splitlines() if "AgentRow.current {" in ln)
    assert "%" not in rule and "$success" in rule


# TUI-21

async def test_home_names_the_keys_a_new_user_needs_and_where_chats_go():
    srv, app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)
        hints = str(app.query_one("#home-hints").render())
        for key in ("/help", "←", "ctrl+j", "shift+tab", "ctrl+x u", "ctrl+x l", "ctrl+p"):
            assert key in hints, key
        assert "files and chats go to" not in hints           # no project loaded
        app.project = "benchmark-game"
        app.refresh_chrome()
        hints = str(app.query_one("#home-hints").render())
        assert "files and chats go to ⌂ benchmark-game" in hints
        assert app.query_one("#home-hints").region.bottom <= 24   # fits the screen


async def test_sessions_picker_does_not_repeat_its_own_hint():
    srv = SessionsServer([{"id": 7, "summary": "one", "started_at": "2026-09-20 10:00:00"}])
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/sessions")
        assert await wait_for(lambda: top(app) == "Picker")
        assert app.screen.hint == ""


# TUI-19, TUI-20

def test_fit_row_drops_whole_hints_by_rank_and_the_right_side_first():
    left = [("enter send", "a", 0), ("ctrl+j newline", "b", 1), ("/ commands", "c", 2),
            ("@ file", "d", 3), ("! shell", "e", 4)]
    right = [("ctrl+x leader", "R1", 2), ("ctrl+p commands", "R2", 3)]
    assert jav3.fit_row(left, right, 120) == ("a · b · c · d · e", "R1 · R2")
    lm, rm = jav3.fit_row(left, right, 73)         # 80 columns less the prompt's margins
    assert (lm, rm) == ("a · b · c · d", "R1")
    lm, rm = jav3.fit_row(left, right, 44)
    assert (lm, rm) == ("a · b", "R1")
    assert jav3.fit_row(left, right, 5) == ("a", "R1")     # each side keeps its first


def status_text(app) -> tuple[str, str]:
    return (str(app.query_one("#status-left").render()),
            str(app.query_one("#status-right").render()))


async def test_80_column_status_row_shows_whole_hints_and_the_armed_esc_message():
    import time
    srv, app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)
        left, right = status_text(app)
        assert left.startswith("enter send · ctrl+j newline · / commands")
        assert "@" not in left.replace("@ file", "") and right.startswith("ctrl+x leader")
        assert len(left) + len(right) + 2 <= app.query_one("#status").size.width
        await open_chat(pilot, app, srv)
        app.unread, app.esc_armed = 5, time.monotonic()
        app._refresh_status()
        left, right = status_text(app)
        assert "esc again to interrupt" in left and right.startswith("● 5 ctrl+b")
        assert len(left) + len(right) + 2 <= app.query_one("#status").size.width
        await finish(srv, app)


async def test_an_idle_status_row_is_not_repainted_ten_times_a_second():
    srv, app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        n = {"left": 0, "right": 0}
        for side in n:
            w = app.query_one(f"#status-{side}")
            real = w.update

            def counted(*a, _real=real, _side=side, **k):
                n[_side] += 1
                return _real(*a, **k)
            w.update = counted
        await pilot.pause(0.8)                     # eight ticks of the app's timer
        assert n == {"left": 0, "right": 0}
        app.unread = 2                             # a change does repaint, once
        await pilot.pause(0.5)
        assert n["right"] == 1 and n["left"] <= 1      # the left may give up a hint
