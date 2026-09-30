"""Terminal client polish: /help as a list (TUI-05), reply-ready toasts (TUI-07),
/sessions rows (TUI-12), the picker hint (TUI-13), code lines (TUI-17), the hint bar
(TUI-19), idle redraws (TUI-20), the home hints (TUI-21), 256-colour rows and the
/security labels."""
import io

import pytest

from cli_fake import FakeServer, finish, load_client, open_chat, send, top, wait_for

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
