"""A dialog that opens by itself while the operator is typing (an agent's ask, a
/local approval) takes none of the keys aimed at the prompt: no answer, no skip.
The keys it turns away go back to the prompt (clients/jav3cli/jav3, class Inert)."""
import pytest

from cli_fake import (FakeServer, ask, composer, finish, load_client, open_chat, plain,
                      top, wait_for)

jav3 = load_client("jav3cli_dialog_keys")


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path / "cfg" / "jav3"


async def test_an_ask_that_just_opened_takes_no_keys_and_they_go_back_to_the_prompt():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_chat(pilot, app, srv)
        srv.feed.put(ask("ask_a", 4))
        assert await wait_for(lambda: top(app) == "AskUser")
        type(app.screen).GRACE = 1.2                  # a slow test machine must not decide this
        type(app.screen).MAX_INERT = 60
        # the operator was typing a mid-turn message: its tail, an enter and an esc
        await pilot.press("s", "o", "o", "n", "1", "enter", "escape", "ctrl+c")
        await pilot.pause(0.1)
        assert top(app) == "AskUser" and srv.answers == []
        assert app.screen.answers == [] and app.screen._text() == ""
        assert composer(app) == "soon1"               # printable keys went back to the prompt
        assert "just opened" in plain(app)
        # a typist who keeps going keeps it shut (each key restarts the pause) ...
        for _ in range(3):
            await pilot.pause(0.6)
            await pilot.press("x")
        assert top(app) == "AskUser" and srv.answers == []
        # ... and once they pause it takes keys as usual
        await pilot.pause(1.4)
        await pilot.press("2", "enter")
        assert await wait_for(lambda: len(srv.answers) == 1)
        assert srv.answers[0]["answers"] == [{"selected": ["No"], "text": None}]
        await finish(srv, app)


async def test_it_gives_up_being_inert_for_a_typist_who_never_pauses():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_chat(pilot, app, srv)
        srv.feed.put(ask("ask_a", 4))
        assert await wait_for(lambda: top(app) == "AskUser")
        type(app.screen).MAX_INERT = 0.8
        for _ in range(6):
            await pilot.press("x")
            await pilot.pause(0.25)
        assert not app.screen.inert()
        await pilot.press("escape")
        assert await wait_for(lambda: srv.answers == [{"id": "ask_a", "skipped": True}])
        await finish(srv, app)


async def test_a_local_approval_that_just_opened_takes_no_keys_either():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        got = []

        async def approve():
            got.append(await app.local_approve("shell", "$ rm -rf build"))
        app.run_worker(approve())
        assert await wait_for(lambda: top(app) == "LocalApprove")
        await pilot.press("y", "a", "enter", "n", "escape", "ctrl+c")
        await pilot.pause(0.1)
        assert top(app) == "LocalApprove" and got == []
        assert composer(app) == "yan"
        await pilot.pause(0.9)
        await pilot.press("n")
        assert await wait_for(lambda: got == ["no"])
