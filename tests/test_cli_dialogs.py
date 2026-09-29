"""The terminal client's ask dialogs (clients/jav3cli/jav3): several agents asking
at once queue up and each names its agent, a dialog that has just opened does not
take the keys the operator was typing, and the permission dialog reads clearly."""
import pytest

from cli_fake import (FakeServer, ask, finish, load_client, open_chat, plain,
                      top, wait_for)

jav3 = load_client("jav3cli_dialogs")


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path / "cfg" / "jav3"


# --- TUI-01: concurrent asks -------------------------------------------------------------

async def test_two_asks_at_once_queue_and_neither_is_lost():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_chat(pilot, app, srv)
        srv.feed.put(ask("ask_a", 5, "Write a.txt?"), ask("ask_b", 6, "Write b.txt?"))
        assert await wait_for(lambda: top(app) == "AskUser")
        type(app.screen).GRACE = 0
        await pilot.pause(0.2)
        # one dialog at a time
        assert [type(s).__name__ for s in app.screen_stack].count("AskUser") == 1
        first = app.screen.ev["id"]
        second = "ask_b" if first == "ask_a" else "ask_a"
        assert "1 more waiting" in plain(app)
        await pilot.press("1", "enter")
        assert await wait_for(lambda: len(srv.answers) == 1)
        assert srv.answers[0]["id"] == first
        # the server settles the answered one; it must not close the other
        srv.feed.put({"type": "ask_done", "id": first, "conversation_id": 5})
        assert await wait_for(lambda: top(app) == "AskUser" and app.screen.ev["id"] == second)
        await pilot.pause(0.2)
        assert top(app) == "AskUser" and app.screen.ev["id"] == second
        assert "more waiting" not in plain(app)
        await pilot.press("2", "enter")
        assert await wait_for(lambda: len(srv.answers) == 2)
        assert srv.answers[1]["id"] == second
        srv.feed.put({"type": "ask_done", "id": second, "conversation_id": 6})
        assert await wait_for(lambda: top(app) != "AskUser")
        await finish(srv, app)


async def test_ask_done_for_a_queued_ask_drops_it_without_a_dialog():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_chat(pilot, app, srv)
        srv.feed.put(ask("ask_a", 5), ask("ask_b", 6))
        assert await wait_for(lambda: top(app) == "AskUser")
        type(app.screen).GRACE = 0
        first = app.screen.ev["id"]
        second = "ask_b" if first == "ask_a" else "ask_a"
        srv.feed.put({"type": "ask_done", "id": second, "conversation_id": 6})
        await pilot.pause(0.3)
        assert "more waiting" not in plain(app)
        await pilot.press("1", "enter")
        assert await wait_for(lambda: len(srv.answers) == 1)
        srv.feed.put({"type": "ask_done", "id": first, "conversation_id": 5})
        assert await wait_for(lambda: top(app) != "AskUser")
        await pilot.pause(0.2)
        assert len(srv.answers) == 1          # the settled ask never got a dialog
        await finish(srv, app)


async def test_a_copy_of_an_answered_ask_arriving_late_is_not_a_new_question():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_chat(pilot, app, srv)
        srv.feed.put(ask("ask_a", 4))
        assert await wait_for(lambda: top(app) == "AskUser")
        type(app.screen).GRACE = 0
        await pilot.press("escape")                       # skipped: answered
        assert await wait_for(lambda: srv.answers)
        assert await wait_for(lambda: top(app) != "AskUser")
        srv.feed.put(ask("ask_a", 4))                     # the same event, on a second channel
        await pilot.pause(0.3)
        assert top(app) != "AskUser" and len(srv.answers) == 1
        await finish(srv, app)


async def test_an_ask_whose_answer_did_not_go_through_shows_again_when_replayed():
    """The server still holds it and replays it when the view re-attaches."""
    srv = FakeServer()
    srv.answer_status = 500
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_chat(pilot, app, srv)
        srv.feed.put(ask("ask_a", 4))
        assert await wait_for(lambda: top(app) == "AskUser")
        type(app.screen).GRACE = 0
        await pilot.press("1", "enter")
        assert await wait_for(lambda: srv.answers)
        assert await wait_for(lambda: top(app) != "AskUser")
        srv.feed.put(ask("ask_a", 4))                     # replayed
        assert await wait_for(lambda: top(app) == "AskUser")
        await pilot.press("escape")
        assert await wait_for(lambda: len(srv.answers) == 2)
        await finish(srv, app)


async def test_ask_done_while_another_dialog_is_on_top_closes_only_its_own():
    """A /local approval sits on top of an ask the server then settles: closing
    the ask must not pop the approval."""
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_chat(pilot, app, srv)
        srv.feed.put(ask("ask_a", 4))
        assert await wait_for(lambda: top(app) == "AskUser")
        type(app.screen).GRACE = 0
        got = []

        async def approve():
            got.append(await app.local_approve("shell", "$ ls"))
        app.run_worker(approve())
        assert await wait_for(lambda: top(app) == "LocalApprove")
        type(app.screen).GRACE = 0
        srv.feed.put({"type": "ask_done", "id": "ask_a", "conversation_id": 4})
        await pilot.pause(0.3)
        assert top(app) == "LocalApprove" and not got
        await pilot.press("y")
        assert await wait_for(lambda: got == ["yes"])
        assert await wait_for(lambda: top(app) not in ("AskUser", "LocalApprove"))
        assert srv.answers == []                  # settled elsewhere: nothing to send
        await finish(srv, app)
