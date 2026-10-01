"""/resume in the terminal client (clients/jav3cli/jav3): continues a turn that died
from its last step. The failure is saved with its tool calls and the server gives
them back to the model on the next message (tests/test_failed_turn_resume.py);
here the client only has to say so, check the chat's last message really is a
failed turn, and send the fixed "continue" message."""
import pytest

from backend import chat as chat_backend
from cli_fake import FakeServer, finish, load_client, open_chat, send, top, wait_for

jav3 = load_client("jav3cli_resume")


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / "cfg" / "jav3"


def notes(app) -> list[str]:
    return [str(n.render()) for n in app.query("Note")]


def saved(srv, cid, *rows, running=False):
    """What GET /api/conversations/<cid>/messages holds: (role, content) rows."""
    srv.conv_messages[cid] = {
        "messages": [{"id": i + 1, "role": role, "content": text, "created_at": "t"}
                     for i, (role, text) in enumerate(rows)],
        "running": running, "pending_activity": [], "agent_slug": None}


async def die(pilot, app, srv, why="guest closed the connection mid-turn"):
    """Send a first message and let its turn fail, the server having saved the failure."""
    await open_chat(pilot, app, srv)
    saved(srv, 4, ("user", "go"), ("assistant", f"(turn failed: {why})"))
    srv.feed.put({"type": "error", "message": why})
    srv.feed.close()
    assert await wait_for(lambda: not app.busy)


async def test_a_failed_turn_says_resume_continues_it():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await die(pilot, app, srv)
        assert any("guest closed the connection" in n for n in notes(app))
        assert any("/resume" in n and "last step" in n for n in notes(app))


async def test_resume_sends_the_fixed_message_to_the_same_chat():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await die(pilot, app, srv)
        await send(pilot, app, "/resume")
        assert await wait_for(lambda: len(srv.posts) == 2)
        assert srv.posts[1]["message"] == chat_backend.RESUME_MESSAGE
        assert srv.posts[1]["conversation_id"] == 4
        srv.feed.put({"type": "start", "conversation_id": 4})
        await finish(srv, app)


async def test_resume_after_a_finished_turn_is_the_sessions_picker_as_before():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        saved(srv, 4, ("user", "go"), ("assistant", "ok"))
        await finish(srv, app)
        await send(pilot, app, "/resume")
        assert await wait_for(lambda: top(app) == "Picker")
        assert app.screen.title_text == "Sessions"
        assert len(srv.posts) == 1                      # nothing was sent
        await pilot.press("escape")


async def test_resume_once_the_chat_has_moved_on_is_the_sessions_picker():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await die(pilot, app, srv)
        saved(srv, 4, ("user", "go"), ("assistant", "(turn failed: boom)"),
              ("user", "what is 2+2"), ("assistant", "4"))
        await send(pilot, app, "/resume")
        assert await wait_for(lambda: top(app) == "Picker")
        assert len(srv.posts) == 1
        await pilot.press("escape")


async def test_resume_with_an_argument_is_the_sessions_command():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await die(pilot, app, srv)
        saved(srv, 7, ("user", "old chat"), ("assistant", "old reply"))
        await send(pilot, app, "/resume 7")
        assert await wait_for(lambda: app.cid == 7)
        assert len(srv.posts) == 1                      # it opened chat 7, it did not continue


async def test_resume_knows_the_older_guest_loop_error_form():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await die(pilot, app, srv)
        saved(srv, 4, ("user", "go"), ("assistant", "(guest loop error: ModelError: 502)"))
        await send(pilot, app, "/resume")
        assert await wait_for(lambda: len(srv.posts) == 2)
        srv.feed.put({"type": "start", "conversation_id": 4})
        await finish(srv, app)


async def test_resume_without_a_chat_is_the_sessions_picker():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "/resume")
        assert await wait_for(lambda: top(app) == "Picker")
        assert not srv.posts
        await pilot.press("escape")


async def test_resume_is_listed_in_the_commands():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        cmds = app.commands
        assert "resume" in cmds and cmds["resume"].fn == app.c_resume
        assert cmds["history"].fn == app.c_sessions         # the other alias is untouched
