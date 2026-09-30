"""The terminal client when the connection drops mid-turn (clients/jav3cli/jav3): it
keeps retrying quietly, re-attaches to a turn that is still running, and draws what
a finished turn did while it was away, each row once. And /logout in two steps."""
import pytest

from cli_fake import FakeServer, call, finish, load_client, open_chat, send, wait_for

jav3 = load_client("jav3cli_reattach")


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(jav3, "RECONNECT_STEPS", (0.05,), raising=False)
    return tmp_path / "cfg" / "jav3"


def notes(app) -> list[str]:
    return [str(n.render()) for n in app.query("Note")]


# --- /logout: who, then yes ------------------------------------------------------------------

async def test_a_bare_logout_only_says_who_and_what_yes_does():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "/logout")
        assert await wait_for(lambda: any("/logout yes" in n for n in notes(app)))
        line = next(n for n in notes(app) if "/logout yes" in n)
        assert "signed in as device:test" in line and "revokes this computer's token" in line
        assert app.token == "jvd_x" and app.logged_in is not False
        assert app.screen_stack[-1] is app.screen_stack[0]        # no dialog either
        await send(pilot, app, "/logout maybe")                   # anything but yes is still step one
        await pilot.pause(0.3)
        assert app.token == "jvd_x"


async def test_logout_yes_signs_out():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "/logout yes")
        assert await wait_for(lambda: app.token is None)
        assert app.logged_in is False


# --- a drop mid-turn ---------------------------------------------------------------------------

READ = call("read_file", {"path": "a.py"}, "print(1)")
BASH = call("bash", {"command": "pytest -q"}, "3 passed")
GREP = call("grep", {"pattern": "TODO"}, "a.py:1: TODO")


def rows(app) -> list[tuple[str, bool]]:
    """The tool rows in the log: (name, finished)."""
    return [(tv.tname, tv.done) for tv in app.query("ToolView")]


def replies(app) -> list[str]:
    return [w.source for w in app.log_view.children if type(w).__name__ == "Reply"]


def footers(app) -> list[str]:
    return [str(w.render()) for w in app.log_view.children if type(w).__name__ == "Footer"]


async def two_calls_then_drop(pilot, app, srv):
    """Turn #4: read_file done, bash started (still spinning), then the server goes away."""
    await open_chat(pilot, app, srv)
    srv.feed.put({"type": "token", "text": "Looking"},
                 {"type": "tool", "id": "t1", "name": "read_file", "args": READ["args"]},
                 {"type": "tool_result", "id": "t1", "name": "read_file", "ok": True,
                  "result": READ["result"]},
                 {"type": "tool", "id": "t2", "name": "bash", "args": BASH["args"]})
    assert await wait_for(lambda: len(rows(app)) == 2)
    srv.drop()
    assert await wait_for(lambda: app.reconnecting)


async def test_a_turn_that_finished_while_away_is_drawn_once_and_the_footer_is_right():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await two_calls_then_drop(pilot, app, srv)
        # the one status line says so; the log does not fill with notes
        assert "reconnecting…" in " ".join(p[0] for p in app._status_parts())
        assert not [n for n in notes(app) if "server" in n or "reconnect" in n]
        app.send("and then the docs")                  # typed while it was down: waits
        assert app.queue == ["and then the docs"]
        srv.back(4, "go", reply="All three checks pass.", activity=[READ, BASH, GREP])
        assert await wait_for(lambda: not app.reconnecting and srv.posts[1:])
        # read_file and bash were on screen already, grep was missed: three rows, each once
        assert rows(app) == [("read_file", True), ("bash", True), ("grep", True)]
        assert replies(app).count("All three checks pass.") == 1
        assert app.query("ToolView")[1].result == "3 passed"          # finished in place
        assert not [n for n in notes(app) if "not sent" in n or "went away" in n]
        # the waiting message went as the next turn, and nothing came back to the prompt
        assert srv.posts[1]["message"] == "and then the docs"
        assert app.editor.text == ""
        srv.feeds[-1].put({"type": "start", "conversation_id": 4})
        await finish(srv, app)
        first = footers(app)[0]
        assert "3 tools" in first and "stream lost" not in first and "failed" not in first


async def test_a_turn_still_running_is_re_attached_without_repeating_a_row():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await two_calls_then_drop(pilot, app, srv)
        srv.back(4, "go", pending=[READ, BASH, GREP], running=True)
        assert await wait_for(lambda: srv.streams.get(4))
        assert await wait_for(lambda: len(rows(app)) == 3)
        feed = srv.streams[4][0]
        # the stream was opened before the transcript was read: the grep that ended
        # in between comes down it as well, and must not be drawn again
        feed.put({"type": "tool", "id": "t3", "name": "grep", "args": GREP["args"]},
                 {"type": "tool_result", "id": "t3", "name": "grep", "ok": True,
                  "result": GREP["result"]},
                 {"type": "tool", "id": "t4", "name": "web_read", "args": {"url": "x"}},
                 {"type": "tool_result", "id": "t4", "name": "web_read", "ok": True,
                  "result": "page"})
        assert await wait_for(lambda: len(rows(app)) == 4)
        await pilot.pause(0.2)
        assert rows(app) == [("read_file", True), ("bash", True), ("grep", True),
                             ("web_read", True)]
        assert not app.reconnecting and app.busy
        feed.put({"type": "final", "content": "Done: four calls.", "conversation_id": 4})
        feed.close()
        assert await wait_for(lambda: not app.busy)
        assert replies(app) == ["Looking", "Done: four calls."]   # the text before the calls stays
        assert len(footers(app)) == 1 and "4 tools" in footers(app)[0]
        assert len(srv.posts) == 1 and app.editor.text == ""                # nothing re-sent


async def test_a_server_back_without_the_reply_says_the_turn_did_not_finish():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await two_calls_then_drop(pilot, app, srv)
        srv.back(4, "go")                    # restarted: the message is saved, no reply
        assert await wait_for(lambda: not app.busy)
        assert any("no saved reply" in n for n in notes(app))
        assert rows(app) == [("read_file", True), ("bash", True)]
        assert not any(not done for _, done in rows(app))        # no spinner left
        assert "failed" in footers(app)[0]
        assert app.editor.text == ""                            # it is in the chat already


async def test_esc_while_reconnecting_gives_up_and_hands_typed_messages_back():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await two_calls_then_drop(pilot, app, srv)
        app.send("one more thing")
        app.interrupt()
        assert await wait_for(lambda: not app.busy)
        line = next(n for n in notes(app) if "mid-turn" in n)
        assert "/sessions" in line
        assert app.editor.text == "one more thing"


async def test_a_server_that_stays_away_is_given_up_on_with_one_line(monkeypatch):
    monkeypatch.setattr(jav3, "RECONNECT_GIVE_UP", 0.4)
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await two_calls_then_drop(pilot, app, srv)
        assert await wait_for(lambda: not app.busy)
        assert len([n for n in notes(app) if "mid-turn" in n]) == 1
        assert not any(not done for _, done in rows(app))
        assert "stream lost" in footers(app)[0]


async def test_a_message_that_never_reached_the_server_still_goes_back_in_the_prompt():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "hello there")
        assert await wait_for(lambda: srv.feeds)
        srv.feed.drop()                      # no `start`: the server never took it
        assert await wait_for(lambda: not app.busy)
        assert any("not sent" in n for n in notes(app))
        assert app.editor.text == "hello there" and not app.reconnecting


async def test_a_dropped_watch_of_a_running_chat_does_not_draw_its_seeded_rows_again():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    srv.conv_messages[4] = {"messages": [{"id": 1, "role": "user", "content": "go"}],
                            "running": True, "pending_activity": [READ], "agent_slug": None}
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await app.open_conversation(4)
        assert await wait_for(lambda: srv.streams.get(4))
        srv.streams[4][0].put({"type": "tool", "id": "b1", "name": "bash", "args": BASH["args"]})
        assert await wait_for(lambda: len(rows(app)) == 2)
        srv.down = True
        srv.streams[4][0].drop()
        assert await wait_for(lambda: app.reconnecting)
        srv.back(4, "go", reply="Done.", activity=[READ, BASH])
        assert await wait_for(lambda: not app.busy)
        assert rows(app) == [("read_file", True), ("bash", True)]
        assert replies(app) == ["Done."]


async def test_text_streaming_at_the_drop_is_replaced_by_the_saved_reply_in_place():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        srv.feed.put({"type": "token", "text": "Lighthouses are t"})
        await pilot.pause(0.3)
        srv.drop()
        assert await wait_for(lambda: app.reconnecting)
        srv.back(4, "go", reply="Lighthouses are tall.")
        assert await wait_for(lambda: not app.busy)
        assert replies(app) == ["Lighthouses are tall."]          # no fragment left beside it
        assert footers(app) and "stream lost" not in footers(app)[0]


async def test_calls_missed_after_streamed_text_close_that_text_with_a_gap_mark():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        srv.feed.put({"type": "token", "text": "Let me chec"})
        await pilot.pause(0.3)
        srv.drop()
        assert await wait_for(lambda: app.reconnecting)
        srv.back(4, "go", reply="All good.", activity=[READ])
        assert await wait_for(lambda: not app.busy)
        assert replies(app) == ["Let me chec …", "All good."]
        assert rows(app) == [("read_file", True)]
