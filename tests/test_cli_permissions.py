"""Permission modes in the terminal client (TUI-04): /permissions and a leader key
choose yolo / auto / ask, /help and the home screen say so, and the default is
explained once, the first time a turn starts with it."""
import json

import pytest

from cli_fake import FakeServer, finish, load_client, open_chat, send, top, wait_for

jav3 = load_client("jav3cli_permissions")


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / "cfg" / "jav3"


def notes(app) -> list[str]:
    return [str(n.render()) for n in app.query("Note")]


async def test_permissions_picker_lists_the_three_modes_and_says_which_is_the_default():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "/permissions")
        assert await wait_for(lambda: top(app) == "Picker")
        rows = app.screen.rows
        assert [r[0] for r in rows] == ["yolo", "auto", "ask"]
        assert "default" in rows[0][1] and "without asking" in rows[0][2]
        assert "judge" in rows[1][2] and "asks you first" in rows[2][2]
        assert "shift+tab" in app.screen.hint
        await pilot.press("down", "down", "enter")
        assert await wait_for(lambda: app.perm_mode == "ask")
        assert any("permissions: ask" in n for n in notes(app))


async def test_permissions_takes_the_mode_as_an_argument_and_saves_it_on_the_chat():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        await send(pilot, app, "/permissions auto")
        assert await wait_for(lambda: app.perm_mode == "auto")
        assert await wait_for(lambda: srv.perm_puts == [{"mode": "auto"}])
        await send(pilot, app, "/perms ASK")
        assert await wait_for(lambda: app.perm_mode == "ask")
        await send(pilot, app, "/mode banana")
        assert await wait_for(lambda: any("not one of yolo, auto, ask" in n for n in notes(app)))
        assert app.perm_mode == "ask"
        await finish(srv, app)


async def test_the_slash_menu_and_the_leader_key_reach_it():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("/", "p", "e", "r", "m")
        await pilot.pause(0.1)
        assert app.popup_items[0] == ("cmd", "permissions")
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert [v for _, v in app.popup_items] == ["yolo", "auto", "ask"]
        app.editor.text = ""
        await pilot.pause(0.1)
        await pilot.press("ctrl+x", "u")
        assert await wait_for(lambda: top(app) == "Picker")
        assert app.screen.title_text == "Permissions"


async def test_help_and_the_home_screen_mention_them():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        hints = str(app.query_one("#home-hints").render())
        assert "ctrl+x u" in hints and "permissions" in hints
        app.dispatch("/help")
        assert await wait_for(lambda: top(app) == "Help")
        text = app.screen.text
        assert "shift+tab" in text and "/permissions" in text and "ctrl+x u" in text


async def test_the_default_is_explained_once_on_the_first_turn():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        assert await wait_for(lambda: any("agents run in the VM without asking" in n
                                          for n in notes(app)))
        await finish(srv, app)
        assert json.loads((cfg_dir() / "tui.json").read_text())["perm_explained"] is True
        n = len([x for x in notes(app) if "without asking" in x])
        await send(pilot, app, "again")
        assert await wait_for(lambda: len(srv.feeds) == 2)
        srv.feeds[-1].put({"type": "start", "conversation_id": 4})
        await finish(srv, app)
        assert len([x for x in notes(app) if "without asking" in x]) == n == 1


async def test_someone_who_already_changed_the_mode_is_not_told():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("shift+tab", "shift+tab", "shift+tab")     # around to yolo again
        await pilot.pause(0.2)
        assert app.perm_mode == "yolo"
        await open_chat(pilot, app, srv)
        await pilot.pause(0.3)
        assert not any("agents run in the VM without asking" in n for n in notes(app))
        await finish(srv, app)


def cfg_dir():
    import os
    from pathlib import Path
    return Path(os.environ["XDG_CONFIG_HOME"]) / "jav3"
