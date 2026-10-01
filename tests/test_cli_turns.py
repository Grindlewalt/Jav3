"""The terminal client around a turn (clients/jav3cli/jav3): a server that goes away
reads as one clear line (TUI-10; a message the server never took returns to the
prompt, one it took is followed: test_cli_reattach.py), a stopped
turn never lends its stream or its stop to the next message (TUI-11), and an
agent's chat has a way back to its parent (TUI-16)."""
import httpx
import pytest

from cli_fake import FakeServer, finish, load_client, open_chat, send, wait_for

jav3 = load_client("jav3cli_turns")


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / "cfg" / "jav3"


def notes(app) -> list[str]:
    return [str(n.render()) for n in app.query("Note")]


def log(app) -> list[tuple[str, str]]:
    """The transcript as (widget class, text), top to bottom."""
    out = []
    for w in app.log_view.children:
        text = w.source if hasattr(w, "source") else str(w.render())
        out.append((type(w).__name__, str(text)))
    return out


# --- TUI-10: the server goes away ----------------------------------------------------------------

async def test_a_restart_mid_turn_reads_as_one_line_and_the_message_stays_in_the_chat():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "Write a 1200-word essay about lighthouses.")
        assert await wait_for(lambda: srv.feeds)
        srv.feed.put({"type": "start", "conversation_id": 4},
                     {"type": "token", "text": "Lighthouses "})
        await pilot.pause(0.2)
        app.editor.text = "and one about docks"              # typed while it ran
        srv.feed.drop()                                      # [Errno 104] Connection reset by peer
        assert await wait_for(lambda: not app.busy)
        # the server took the message (its `start` came), so it is not put back: the client
        # reconnected, found no reply saved, and says so once (test_cli_reattach.py has the
        # cases where the turn is picked up again)
        lines = [n for n in notes(app) if "no saved reply" in n]
        assert len(lines) == 1 and "Your message is in the chat" in lines[0]
        assert not any("Errno" in n for n in notes(app))
        assert app.editor.text == "and one about docks"


async def test_an_unreachable_server_says_so_and_nothing_is_lost():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        srv.post_error = httpx.ConnectError("All connection attempts failed")
        await send(pilot, app, "hello there")
        assert await wait_for(lambda: any("can't reach" in n for n in notes(app)))
        line = next(n for n in notes(app) if "can't reach" in n)
        assert "not sent" in line and "back in the prompt" in line
        assert "All connection attempts failed" not in line
        assert app.editor.text == "hello there" and not app.busy
        assert list(app.query("UserMsg")[0].classes) == ["failed"]
        # the server comes back: the same words go through
        srv.post_error = None
        await pilot.press("enter")
        assert await wait_for(lambda: srv.feeds)
        srv.feed.put({"type": "start", "conversation_id": 4})
        await finish(srv, app)
        assert srv.posts[-1]["message"] == "hello there"


async def test_a_stream_that_just_closes_with_no_final_is_a_lost_stream_too():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "write it")
        assert await wait_for(lambda: srv.feeds)
        srv.feed.put({"type": "start", "conversation_id": 4}, {"type": "token", "text": "Hm"})
        srv.feed.close()                                     # a clean close, no final
        assert await wait_for(lambda: any("no saved reply" in n for n in notes(app)))
        assert await wait_for(lambda: not app.busy)
        assert app.editor.text == ""


async def test_a_refused_send_keeps_the_text_in_the_prompt():
    srv = FakeServer()
    srv.post_status = 404
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "first message")
        assert await wait_for(lambda: any("server said 404" in n for n in notes(app)))
        assert app.editor.text == "first message" and not app.busy


# --- TUI-11: a stopped turn and the next message --------------------------------------------------

async def test_after_an_interrupt_the_next_message_waits_for_the_old_turn_and_stays_apart():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        srv.feed.put({"type": "token", "text": "The harbor essay begins"})
        await pilot.pause(0.3)
        old = srv.feed
        await pilot.press("escape", "escape")
        assert await wait_for(lambda: srv.stops == ["/api/chat/4/stop"])
        # the server is slow to wind the turn down; meanwhile they type the next one
        await send(pilot, app, "Count from 1 to 400")
        await pilot.pause(0.3)
        assert len(srv.posts) == 1 and app.queue == ["Count from 1 to 400"]
        # a late token of the old turn, then its interrupted final
        old.put({"type": "token", "text": " and goes on."},
                {"type": "final", "content": "[Request interrupted by the operator]",
                 "conversation_id": 4})
        old.close()
        assert await wait_for(lambda: len(srv.posts) == 2)
        assert srv.posts[1] == {"message": "Count from 1 to 400", "conversation_id": 4}
        new = srv.feed
        assert new is not old
        new.put({"type": "start", "conversation_id": 4}, {"type": "token", "text": "1 2 3"},
                {"type": "final", "content": "1 2 3", "conversation_id": 4})
        new.close()
        assert await wait_for(lambda: not app.busy)
        kinds = [(k, t) for k, t in log(app) if k in ("UserMsg", "Reply")]
        texts = [t for _, t in kinds]
        assert texts.index("Count from 1 to 400") > max(
            i for i, t in enumerate(texts) if "harbor essay" in t)
        assert not any("harbor" in t for t in texts[texts.index("Count from 1 to 400"):])
        assert texts[-1] == "1 2 3"


async def test_a_stop_asked_for_one_turn_never_stops_another():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        await pilot.pause(0.2)
        app.turn.cid = 9                                    # the next turn is another chat
        await app._stop_remote(4)                           # a stop that was asked for #4
        assert srv.stops == ["/api/chat/4/stop"]
        app.turn.cid = 4
        await finish(srv, app)


async def test_a_stream_that_no_longer_belongs_to_the_turn_on_screen_is_dropped():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        first = jav3.TurnState()
        app.turn, app.busy = first, True
        seen = []

        async def lines():
            yield 'data: {"type": "token", "text": "one "}'
            yield ""
            app.turn = second                                # the view moved to another turn
            yield 'data: {"type": "token", "text": "stale"}'
            yield ""
        second = jav3.TurnState()
        real = app.handle_event

        async def spy(ev):
            seen.append(ev["text"])
            return await real(ev)
        app.handle_event = spy
        await app._consume(lines())
        assert seen == ["one "]
        app.turn, app.busy = None, False


# --- TUI-16: an agent's chat and the way back --------------------------------------------------------

BRIEF = ("You are agent item i1 of the plan. Write c.txt with the\ncontent hello and then report "
         "back to\nthe orchestrator.\n\n- keep it short\n- no extra files\n\nDone means the file exists.")


def _agent_chat(srv):
    srv.conv_messages[7] = {"messages": [{"id": 1, "role": "user", "content": BRIEF,
                                          "created_at": "2026-09-29 10:00:00"}],
                            "running": True, "pending_activity": [], "agent_slug": None}
    srv.conv_messages[3] = {"messages": [{"id": 2, "role": "user", "content": "orchestrate it",
                                          "created_at": "2026-09-29 09:00:00"}],
                            "running": False, "pending_activity": [], "agent_slug": None}


def test_reflow_joins_wrapped_lines_and_keeps_lists_and_code():
    assert jav3.reflow("one two\nthree four\n\nnext para") == "one two three four\n\nnext para"
    text = "intro line\nsecond\n- a\n- b\n\n    code()\nmore"
    assert jav3.reflow(text) == "intro line second\n- a\n- b\n\n    code()\nmore"
    assert jav3.reflow("1. one\n2. two\n```\nx\ny\n```") == "1. one\n2. two\n```\nx\ny\n```"
    assert jav3.reflow("") == ""


async def test_esc_in_an_agents_chat_goes_back_to_its_parent_and_ctrl_c_stops_it():
    srv = FakeServer()
    _agent_chat(srv)
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await app.open_node({"id": 7, "parent_id": 3, "running": True, "kind": "item"})
        assert await wait_for(lambda: app.busy and srv.streams.get(7))
        assert app.cid == 7 and app.child_of == 3
        assert any("esc goes back to #3, ctrl+c stops it" in n for n in notes(app))
        # the brief reads as paragraphs, wrapped by the terminal
        brief = next(t for k, t in log(app) if k == "UserMsg")
        assert brief.startswith("You are agent item i1 of the plan. Write c.txt with the content")
        assert "- keep it short\n- no extra files" in brief
        # esc once: back to the orchestrator, the agent keeps running, nothing is stopped
        await pilot.press("escape")
        assert await wait_for(lambda: app.cid == 3)
        assert app.child_of is None and srv.stops == []
        assert not app.busy
        # ctrl+c is the way to stop it
        await app.open_node({"id": 7, "parent_id": 3, "running": True, "kind": "item"})
        assert await wait_for(lambda: app.busy and len(srv.streams.get(7, [])) == 2)
        await pilot.press("ctrl+c")
        assert await wait_for(lambda: srv.stops == ["/api/chat/7/stop"])
        srv.streams[7][-1].close()
        await wait_for(lambda: not app.busy)


async def test_esc_in_an_ordinary_running_chat_still_needs_two_presses_to_interrupt():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert srv.stops == [] and app.child_of is None
        await pilot.press("escape")
        assert await wait_for(lambda: srv.stops == ["/api/chat/4/stop"])
        srv.feed.put({"type": "final", "content": "[Request interrupted by the operator]",
                      "conversation_id": 4})
        srv.feed.close()
        await wait_for(lambda: not app.busy)
