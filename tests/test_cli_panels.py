"""jav3.2 P1: panels in the terminal client. The area above the prompt tiles like tmux (a
split tree, at most four panels); every panel has a PageHost of its own with a chat at the
bottom of it, the chat state is per panel (several chats stream at once), one prompt line
types into the focused panel, and ctrl+x plus an arrow / 1-4 / z / 0 moves, jumps, zooms and
closes. The first half is the pure layout (no terminal); the second drives the Textual app.
The client file is loaded from tests/cli_fake.py."""
import pytest

from cli_fake import FakeServer, ask, load_client, send, wait_for

jav3 = load_client("jav3cli_panels")


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))


# -- the split tree (pure) -----------------------------------------------------------------

S = jav3.PanelSplit


def test_a_split_puts_the_new_panel_beside_the_target_and_they_share_its_space():
    t = jav3.tree_split(1, 1, 2, "row")
    assert t == S("row", [1, 2]) and t.sizes == [0.5, 0.5]
    assert jav3.tree_split(1, 1, 2, "col") == S("col", [1, 2])
    assert jav3.tree_split(1, 1, 2, "row", before=True) == S("row", [2, 1])
    # a split along the direction the parent already has joins it, halving the target's share
    t3 = jav3.tree_split(t, 2, 3, "row")
    assert t3 == S("row", [1, 2, 3]) and t3.sizes == pytest.approx([0.5, 0.25, 0.25])
    # across it, the target becomes a split of its own
    t3 = jav3.tree_split(t, 1, 3, "col")
    assert t3 == S("row", [S("col", [1, 3]), 2])
    assert jav3.tree_leaves(t3) == [1, 3, 2]
    # an unknown target: beside the last panel
    assert jav3.tree_leaves(jav3.tree_split(t, 9, 3, "col")) == [1, 2, 3]


def test_closing_gives_the_space_to_the_sibling_before_it_and_normalizes():
    t = jav3.tree_split(jav3.tree_split(1, 1, 2, "row"), 1, 3, "col")   # (1 over 3) | 2
    assert jav3.tree_close(t, 3) == S("row", [1, 2])                       # the col collapses
    assert jav3.tree_close(t, 2) == S("col", [1, 3])
    assert jav3.tree_close(t, 1) == S("row", [3, 2])
    only = jav3.tree_close(S("row", [1, 2]), 2)
    assert only == 1                                                      # a split of one is the leaf
    assert jav3.tree_close(1, 1) is None                                  # the last panel: nothing left
    row = jav3.tree_split(jav3.tree_split(1, 1, 2), 2, 3)                 # 1 | 2 | 3
    closed = jav3.tree_close(row, 2)
    assert closed.sizes == pytest.approx([0.75, 0.25])                    # 1 took the quarter 2 had
    # a split inside a split of the same direction is flattened
    nested = S("row", [1, S("row", [2, 3], [0.5, 0.5])], [0.5, 0.5])
    flat = jav3.tree_normalize(nested)
    assert flat == S("row", [1, 2, 3]) and flat.sizes == pytest.approx([0.5, 0.25, 0.25])


def test_rects_cover_the_area_exactly_and_neighbours_follow_the_geometry():
    t = jav3.tree_split(jav3.tree_split(1, 1, 2, "row"), 1, 3, "col")   # (1 over 3) | 2
    r = jav3.tree_rects(t, 81, 25)
    assert r[2] == (41, 0, 40, 25)                       # 81 cut in 41 + 40 (rounded up first)
    assert r[1][:3] == (0, 0, 41) and r[3][:3] == (0, r[1][3], 41)
    assert r[1][3] + r[3][3] == 25
    assert sum(x[2] * x[3] for x in r.values()) == 81 * 25
    # neighbours: 2 is right of both 1 and 3; 3 is below 1; nothing beyond the edges
    n = jav3.tree_neighbor
    assert n(t, 1, "right", 81, 25) == 2 and n(t, 3, "right", 81, 25) == 2
    assert n(t, 2, "left", 81, 25) in (1, 3)
    assert n(t, 1, "down", 81, 25) == 3 and n(t, 3, "up", 81, 25) == 1
    assert n(t, 1, "left", 81, 25) is None and n(t, 2, "right", 81, 25) is None
    assert n(t, 2, "down", 81, 25) is None and n(t, 1, "up", 81, 25) is None
    assert n(1, 1, "right", 80, 24) is None and n(t, 9, "left", 80, 24) is None


def test_the_cut_follows_the_longer_side_and_refuses_when_nothing_would_fit():
    d = jav3.panel_split_dir
    assert d(80, 21) == "row"        # wide: side by side
    assert d(40, 21) == "col"        # a cell is twice as tall as wide: this one is taller
    assert d(160, 41) == "row"
    assert d(79, 40) == "col"
    # too small the long way: the other way, else refused
    assert d(40, 10) is None         # neither half would reach 28 x 8 (or 56 x 8 / 40 x 16)
    assert d(50, 30) == "col"        # side by side would leave 25 columns
    assert d(60, 7) is None


# -- the app ------------------------------------------------------------------------------

def make(srv=None, **kw):
    srv = srv or FakeServer()
    return srv, jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport(), **kw)


def notes(app) -> list[str]:
    return [str(n.render()) for n in app.query("Note")]


def titles(app) -> dict:
    return {n: c.panel.border_title or "" for n, c in app.chats.items()}


async def split(pilot, app, line="/new-panel", want=2):
    app.dispatch(line)
    assert await wait_for(lambda: len(app.chats) == want and app.panel_area.size.width), line
    await pilot.pause(0.2)


async def test_one_panel_looks_as_it_always_did():
    srv, app = make()
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        assert list(app.chats) == [1] and app.focus_no == 1 and not app.zoomed
        panel = app.focused_panel
        assert panel.region.size == app.panel_area.size          # fills the area, no frame
        assert not app.panel_area.has_class("-multi")
        assert str(panel.styles.border.top[0] if panel.styles.border.top else "") in ("", "none")
        assert not app.query_one("#bottom").has_class("multi")
        assert "Ask anything" in app.editor.placeholder


async def test_new_panel_splits_the_focused_one_with_a_new_chat_and_takes_the_focus():
    srv, app = make(project="p1")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        app.chat.cid = 512
        await split(pilot, app)
        assert app.focus_no == 2 and sorted(app.chats) == [1, 2]
        assert app.chats[2].cid is None and app.chats[1].cid == 512
        assert app.chats[2].project == "p1"                       # same project, new chat
        assert app.panel_area.has_class("-multi")
        r1, r2 = app.chats[1].panel.region, app.chats[2].panel.region
        assert r1.width + r2.width == app.panel_area.size.width and r1.x == 0 and r2.x == r1.width
        assert "ask a new chat (panel 2)" in app.editor.placeholder
        assert "/page [args] [panel]" in app.editor.placeholder
        t = titles(app)
        assert t[1].startswith("1 chat #512") and t[2].startswith("2 chat (new)")
        # the sidebar gives way while there are several panels; so does the title row
        app.sidebar_pref = True
        app.refresh_chrome()
        assert not app.sidebar_shown() and not app.query_one("#sidebar").display
        assert all(not c.header.display for c in app.chats.values())
        assert app.query_one("#bottom").has_class("multi")
        assert app.focused_panel.has_class("-focused") and not app.chats[1].panel.has_class("-focused")


async def test_the_prompt_types_into_the_focused_panels_chat_and_two_chats_stream_at_once():
    srv, app = make()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "first question")
        assert await wait_for(lambda: len(srv.feeds) == 1)
        srv.feeds[0].put({"type": "start", "conversation_id": 4},
                         {"type": "token", "text": "answer one "})
        assert await wait_for(lambda: app.chats[1].cid == 4)
        await split(pilot, app)
        await send(pilot, app, "second question")                  # panel 2 is focused now
        assert await wait_for(lambda: len(srv.feeds) == 2)
        assert [p["conversation_id"] for p in srv.posts] == [None, None]
        assert [p["message"] for p in srv.posts] == ["first question", "second question"]
        srv.feeds[1].put({"type": "start", "conversation_id": 5},
                         {"type": "token", "text": "answer two "})
        assert await wait_for(lambda: app.chats[2].cid == 5)
        # both run; the focus is on 2, and 1 goes on streaming into ITS transcript
        assert app.chats[1].busy and app.chats[2].busy
        srv.feeds[0].put({"type": "token", "text": "and more of one"})
        assert await wait_for(lambda: "and more of one" in
                              " ".join(r.source for r in app.chats[1].log.query("Reply")))
        assert not any("and more of one" in r.source for r in app.chats[2].log.query("Reply"))
        assert "answer two" in " ".join(r.source for r in app.chats[2].log.query("Reply"))
        t = titles(app)
        assert "● running" in t[1] and "● running" in t[2]
        # the status row and the meta row are the focused panel's; app.cid is its chat's
        assert app.cid == 5 and app.turn is app.chats[2].turn and app.turn is not app.chats[1].turn
        # moving the focus never cancels a stream
        app.focus_panel(1)
        await pilot.pause(0.1)
        assert app.cid == 4 and app.chats[1].busy and app.chats[2].busy
        # a message typed now is a mid-turn message to chat 4, not 5
        await send(pilot, app, "one more thing")
        assert await wait_for(lambda: srv.messages)
        assert [c for c in srv.calls if c[1].endswith("/message")] == [("POST", "/api/chat/4/message")]
        srv.feeds[0].put({"type": "final", "content": "done one", "conversation_id": 4})
        srv.feeds[0].close()
        assert await wait_for(lambda: not app.chats[1].busy)
        assert app.chats[2].busy and "● running" in titles(app)[2] and "● running" not in titles(app)[1]
        srv.feeds[1].put({"type": "final", "content": "done two", "conversation_id": 5})
        srv.feeds[1].close()
        assert await wait_for(lambda: not app.chats[2].busy)


async def test_a_page_opens_in_a_new_panel_with_the_trailing_word_panel():
    srv, app = make()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await split(pilot, app, "/vms images panel")
        assert app.focus_no == 2
        host2, host1 = app.chats[2].panel.host, app.chats[1].panel.host
        assert type(host2.current).__name__ == "VmsPage" and host2.current.args == ["images"]
        assert type(host1.current).__name__ == "ChatPage"          # the first panel is untouched
        assert titles(app)[2].startswith("2 /vms images")
        assert app.paged and app.top is host2.current
        # the same line without `panel` replaces the view in the focused panel
        app.dispatch("/security calls")
        assert await wait_for(lambda: type(host2.current).__name__ == "SecurityPage")
        assert len(app.chats) == 2
        # `/work panel` is a new chat, /sessions panel opens what you pick in a new panel
        srv.conv_messages[7] = {"messages": [{"id": 1, "role": "user", "content": "old chat",
                                              "created_at": "t"}], "running": False,
                                "pending_activity": [], "agent_slug": None}
        await split(pilot, app, "/sessions 7 panel", want=3)
        assert await wait_for(lambda: app.chats[3].cid == 7)
        assert any("old chat" in str(w.render()) for w in app.chats[3].log.query("UserMsg"))
        assert app.focus_no == 3


async def test_four_panels_is_the_most_and_the_fifth_is_refused_in_one_line():
    srv, app = make()
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause(0.3)
        for want in (2, 3, 4):
            await split(pilot, app, "/new-panel", want=want)
        assert sorted(app.chats) == [1, 2, 3, 4]
        app.dispatch("/new-panel")
        assert await wait_for(lambda: any("4 panels is the most" in n for n in notes(app)))
        assert len(app.chats) == 4
        app.dispatch("/vms panel")
        assert await wait_for(lambda: sum("4 panels is the most" in n for n in notes(app)) == 2)
        assert len(app.chats) == 4
        # every panel has a rect inside the area and none overlaps another
        area = app.panel_area.size
        regs = [c.panel.region for c in app.chats.values()]
        assert all(r.right <= area.width and r.bottom <= area.height for r in regs)
        assert sum(r.width * r.height for r in regs) == area.width * area.height
        # a closed panel's number is free again and the next new panel takes it
        await pilot.press("ctrl+x", "2")
        await pilot.press("ctrl+x", "0")
        assert await wait_for(lambda: sorted(app.chats) == [1, 3, 4])
        await split(pilot, app, "/new-panel", want=4)
        assert sorted(app.chats) == [1, 2, 3, 4] and app.focus_no == 2


async def test_ctrl_x_arrows_move_the_focus_by_geometry_and_digits_jump():
    srv, app = make()
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause(0.3)
        await split(pilot, app, "/new-panel", want=2)          # 1 | 2
        app.focus_panel(1)
        await split(pilot, app, "/new-panel", want=3)          # (1 over 3) | 2, 3 focused
        assert app.focus_no == 3 and app.chats[3].panel.region.x == 0
        assert app.chats[3].panel.region.y > app.chats[1].panel.region.y
        await pilot.press("ctrl+x", "up")
        assert app.focus_no == 1
        await pilot.press("ctrl+x", "right")
        assert app.focus_no == 2
        await pilot.press("ctrl+x", "left")
        assert app.focus_no in (1, 3)
        await pilot.press("ctrl+x", "down")
        assert app.focus_no == 3
        await pilot.press("ctrl+x", "down")                    # nothing below: stays, says so
        assert app.focus_no == 3
        await pilot.press("ctrl+x", "2")
        assert app.focus_no == 2
        await pilot.press("ctrl+x", "4")                       # no panel 4: stays
        assert app.focus_no == 2
        await pilot.press("ctrl+x", "1")
        assert app.focus_no == 1
        # the keyboard is where the panel wants it: the prompt, for a chat
        assert app.focused is app.editor
        # the leader letters still work beside the panel keys
        await pilot.press("ctrl+x", "n")
        assert app.focus_no == 1 and app.cid is None


async def test_ctrl_x_z_zooms_the_focused_panel_and_back_and_0_closes_it():
    srv, app = make()
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("ctrl+x", "z")                       # one panel: nothing to zoom
        assert not app.zoomed
        await split(pilot, app, "/new-panel", want=2)
        full = app.panel_area.size
        await pilot.press("ctrl+x", "z")
        await pilot.pause(0.1)
        assert app.zoomed and app.chats[2].panel.region.size == full
        assert not app.chats[1].panel.display
        assert "zoomed" in titles(app)[2]
        await pilot.press("ctrl+x", "1")                       # zoom follows the focus
        await pilot.pause(0.1)
        assert app.zoomed and app.chats[1].panel.display and not app.chats[2].panel.display
        await pilot.press("ctrl+x", "z")
        await pilot.pause(0.1)
        assert not app.zoomed and app.chats[1].panel.display and app.chats[2].panel.display
        assert app.chats[1].panel.region.width < full.width
        # close the focused one: the other takes all the room and the focus
        await pilot.press("ctrl+x", "0")
        assert await wait_for(lambda: list(app.chats) == [2])
        assert app.focus_no == 2 and app.chats[2].panel.region.size == app.panel_area.size
        assert not app.panel_area.has_class("-multi")
        # the last panel cannot be closed
        await pilot.press("ctrl+x", "0")
        await pilot.pause(0.2)
        assert list(app.chats) == [2] and app.chats[2].panel.is_attached


async def test_closing_a_panel_leaves_its_turn_running_on_the_server():
    srv, app = make()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await split(pilot, app, "/new-panel", want=2)
        await send(pilot, app, "a long job")                   # in panel 2
        assert await wait_for(lambda: srv.feeds)
        srv.feed.put({"type": "start", "conversation_id": 9}, {"type": "token", "text": "working "})
        assert await wait_for(lambda: app.chats[2].cid == 9)
        gone = app.chats[2]
        await pilot.press("ctrl+x", "0")
        assert await wait_for(lambda: list(app.chats) == [1])
        assert not gone.panel.is_attached and not gone.busy
        assert srv.stops == [] and not any(c[1].endswith("/stop") for c in srv.calls)
        assert app.focus_no == 1 and not app.chats[1].busy
        # the server never heard about it: the turn goes on there (nothing was stopped)
        await pilot.pause(0.2)
        srv.feed.close()


async def test_a_dialog_for_an_unfocused_panels_turn_opens_over_everything_with_its_number():
    srv, app = make()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "go")
        assert await wait_for(lambda: srv.feeds)
        srv.feeds[0].put({"type": "start", "conversation_id": 4})
        assert await wait_for(lambda: app.chats[1].cid == 4)
        await split(pilot, app, "/new-panel", want=2)           # focus on 2
        srv.feeds[0].put(ask("a1", 4, "Delete the build dir?", kind="permission"))
        assert await wait_for(lambda: type(app.screen).__name__ == "AskUser")
        head = " ".join(str(w.render()) for w in app.screen.query("Static"))
        assert "panel 1" in head and "chat #4" in head
        assert "needs input" in titles(app)[1] and "needs input" not in titles(app)[2]
        type(app.screen).GRACE = 0                              # no settling time for the keys
        await pilot.press("1")                                  # the first option
        await pilot.pause(0.1)
        await pilot.press("enter")
        assert await wait_for(lambda: type(app.screen).__name__ != "AskUser")
        assert await wait_for(lambda: ("POST", "/api/chat/4/answer") in srv.calls)
        assert app.focus_no == 2                                # the focus never moved
        assert await wait_for(lambda: "needs input" not in titles(app)[1])
        srv.feeds[0].put({"type": "final", "content": "ok", "conversation_id": 4})
        srv.feeds[0].close()
        assert await wait_for(lambda: not app.chats[1].busy)


async def test_a_page_in_another_panel_does_not_steal_the_keyboard():
    srv, app = make()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await split(pilot, app, "/new-panel", want=2)
        host1 = app.chats[1].panel.host
        # a page asked for in panel 1's host while panel 2 has the focus
        await app.open_page("vms", "", target=app.chats[1].panel)
        assert type(host1.current).__name__ == "VmsPage"
        assert app.focus_no == 2 and app.focused is app.editor
        # esc on the focused panel's chat does not touch panel 1's page
        app.focus_panel(1)
        await pilot.pause(0.2)
        assert app.focused is host1.current                    # its page has the keys now
        assert app.paged and "panel 1" in app.editor.placeholder


async def test_a_small_terminal_still_splits_and_refuses_when_there_is_no_room():
    srv, app = make()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)
        await split(pilot, app, "/new-panel", want=2)           # side by side: 40 columns each
        w = app.panel_area.size.width
        assert app.chats[1].panel.region.width + app.chats[2].panel.region.width == w
        assert all(c.panel.region.width >= jav3.PANEL_MIN_COLS for c in app.chats.values())
        await split(pilot, app, "/new-panel", want=3)           # 2 is too narrow to halve: stacked
        r2, r3 = app.chats[2].panel.region, app.chats[3].panel.region
        assert r3.y > r2.y and r3.width == r2.width
        assert all(c.panel.region.height >= jav3.PANEL_MIN_ROWS for c in app.chats.values())
        app.dispatch("/new-panel")                              # 3 is 40 x ~10: no half fits
        assert await wait_for(lambda: any("no room for another panel" in n for n in notes(app)))
        assert len(app.chats) == 3 and "ctrl+x z" in " ".join(notes(app))
        app.focus_panel(1)
        await split(pilot, app, "/new-panel", want=4)           # panel 1 is tall enough
        assert sorted(app.chats) == [1, 2, 3, 4]


async def test_a_window_shrunk_below_what_the_tiles_need_shows_the_focused_panel_alone():
    srv, app = make()
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        await split(pilot, app, "/new-panel", want=2)
        await pilot.resize_terminal(30, 14)
        await pilot.pause(0.3)
        assert app.cramped and app.chats[2].panel.display and not app.chats[1].panel.display
        await pilot.resize_terminal(120, 36)
        await pilot.pause(0.3)
        assert not app.cramped and app.chats[1].panel.display


async def test_a_page_in_a_panel_opens_chats_in_its_own_panel_and_p_opens_a_new_one():
    srv, app = make()
    srv.conv_messages[7] = {"messages": [{"id": 1, "role": "user", "content": "chat seven",
                                          "created_at": "t"}], "running": False,
                            "pending_activity": [], "agent_slug": None}
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await split(pilot, app, "/new-panel", want=2)
        node = {"id": 7, "title": "chat seven", "running": False}
        await app.open_node(node, panel=app.chats[1].panel)     # not the focused one
        assert app.chats[1].cid == 7 and app.chats[2].cid is None
        assert app.focus_no == 2
        await app.open_node({"id": 7, "title": "chat seven"}, new_panel=True)
        assert sorted(app.chats) == [1, 2, 3] and app.chats[3].cid == 7 and app.focus_no == 3
