"""jav3.2 P2: the drawers in the terminal client. ← on an empty prompt slides the AGENTS
drawer over the panels from the left (what runs and what waits on the operator, then the
few that finished), → the SESSIONS drawer from the right (active and needs-input chats on
top). Up and down move, enter opens the entry in the focused panel, p in a new panel (not
at four), esc closes; the keyboard is the drawer's but the focused panel, and a turn
streaming in it, are not touched. The drawers read the app's AgentsFeed while open and
reload at once on the shared stream's run_end. The full /agents page and the /sessions
picker stay (tests/test_cli_agents_live.py, test_cli_pages.py).
JAV3_CLIENT=<path> runs this file against another copy of the client."""
import httpx
import pytest

from cli_fake import FakeServer, Feed, load_client, send, wait_for

jav3 = load_client("jav3cli_drawers")
FULL = jav3.SESSION_PREFIX + "jwt"


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))


def node(nid, title, parent=None, kind="agent", status="running", **kw):
    return {"id": nid, "parent_id": parent, "kind": kind, "title": title, "summary": title,
            "role": kw.pop("role", kind), "status": status, "needs": kw.pop("needs", None),
            "agent_slug": None, "project": kw.pop("project", "shop"), "model": None,
            "running": status in ("running", "needs_you"),
            "started_at": kw.pop("started_at", "2026-09-30 04:00:00"),
            "ended_at": kw.pop("ended_at", None), **kw}


def conv(cid, summary, **kw):
    return {"id": cid, "summary": summary, "title": None, "kind": "chat",
            "project_slug": kw.pop("project_slug", "shop"), "model": None,
            "started_at": kw.pop("started_at", "2026-09-30 04:30:00"), **kw}


class DrawerServer(FakeServer):
    """What the drawers read: the agents tree (active and the finished page), the chat list
    and /api/chat/running, all changeable while the app runs; /api/events when the app
    holds a session (the test feeds it by hand)."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.active: list[dict] = []
        self.finished: list[dict] = []
        self.convs: list[dict] = []
        self.queries: list[dict] = []
        self.events: list[Feed] = []
        self.event_paths: list[str] = []
        self.event_status = 200

    def say(self, event: dict, topic: str = "runs") -> None:
        self.events[-1].put({"topic": topic, "event": event})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        if path == "/api/events":
            self.event_paths.append(str(request.url.query, "ascii"))
            if self.event_status != 200 and "runs" in q.get("topics", ""):
                return httpx.Response(self.event_status, json={"detail": "unknown topic(s): runs"})
            feed = Feed()
            feed.put({"topic": "security", "event": {"type": "stream_open"}})
            self.events.append(feed)
            return httpx.Response(200, stream=feed, headers={"content-type": "text/event-stream"})
        if path == "/api/chat/agents":
            self.calls.append((request.method, path))
            self.queries.append(q)
            if q.get("scope") == "finished":
                roots = [n for n in self.finished if n["parent_id"] is None]
                limit = int(q.get("limit") or 500)
                keep = {n["id"] for n in roots[:limit]}
                nodes = [n for n in self.finished if n["id"] in keep or n["parent_id"] in keep]
                return httpx.Response(200, json={"nodes": nodes, "total": len(roots)})
            return httpx.Response(200, json={"nodes": self.active})
        if path == "/api/conversations" and request.method == "GET":
            self.calls.append((request.method, path))
            return httpx.Response(200, json={"conversations": self.convs})
        return super().handle(request)


def make(srv=None, token="jvd_x", **kw):
    srv = srv or DrawerServer(full=token == FULL)
    return srv, jav3.build_tui("http://h:1", token, transport=srv.transport(), **kw)


def seeded(**kw):
    """A web chat running, a plan with a sub-agent that waits, an old chat, two finished."""
    srv = DrawerServer(**kw)
    srv.running = [41]
    srv.convs = [conv(41, "fix the invoice export", started_at="2026-09-30 05:00:00"),
                 conv(7, "old chat", started_at="2026-09-29 05:00:00"),
                 conv(8, "older chat", started_at="2026-09-28 05:00:00")]
    srv.active = [
        node(10, "Ship the login page", kind="orchestrator", project="site",
             started_at="2026-09-30 06:00:00"),
        node(12, "Build the form", parent=10, role="item i1", project="site"),
        node(13, "Check the copy", parent=10, role="item i2", project="site",
             status="needs_you", needs="approve the wording"),
        node(41, "fix the invoice export", kind="chat", role="chat"),
    ]
    srv.finished = [
        node(5, "Fetch the morning quotes", status="done", role="@morning-stocks",
             started_at="2026-09-30 03:00:00", ended_at="2026-09-30 03:05:00"),
        node(4, "Audit the invoices", status="failed", started_at="2026-09-29 03:00:00",
             ended_at="2026-09-29 03:30:00"),
    ]
    for cid in (5, 4, 7, 8):
        srv.conv_messages[cid] = {"messages": [
            {"id": 1, "role": "user", "content": f"chat {cid}", "created_at": "t0"}],
            "running": False, "pending_activity": [], "agent_slug": None}
    return srv


def drawer_rows(app):
    return [(r.kind, r.key, str(r.render())) for r in app.drawer.rows]


def has(app, *needles):
    text = " ".join(t for _, _, t in drawer_rows(app)) if app.drawer else ""
    return all(n in text for n in needles)


def under(app, section, needle):
    """Is the row that says `needle` under the section head `section`?"""
    cur = None
    for kind, _, text in drawer_rows(app):
        if kind == "section":
            cur = text.split("  ")[0].strip()
        elif needle in text:
            return cur == section
    return False


async def ready(pilot, app, kind="agents", n=3):
    """Open a drawer with a key and wait for its first rows."""
    await pilot.pause(0.3)
    await pilot.press("left" if kind == "agents" else "right")
    assert await wait_for(lambda: app.drawer is not None and app.drawer.kind == kind
                          and len(app.drawer.rows) > n and app.drawer.fetched)
    await pilot.pause(0.1)
    return app.drawer


async def split(pilot, app, want=2):
    app.dispatch("/new-panel")
    assert await wait_for(lambda: len(app.chats) == want and app.panel_area.size.width)
    await pilot.pause(0.2)


def sel(app):
    return app.drawer.sel if app.drawer else None


# -- the pure parts ---------------------------------------------------------------------------

def test_a_drawer_is_about_40_columns_and_narrower_where_a_panel_must_stay_visible():
    assert jav3.drawer_width(160) == 40 and jav3.drawer_width(120) == 40
    assert jav3.drawer_width(80) == 36 and jav3.drawer_width(60) == 27
    assert jav3.drawer_width(30) == 24 and 80 - jav3.drawer_width(80) >= jav3.PANEL_MIN_COLS


def test_agent_rows_are_running_first_with_needs_you_after_then_the_newest_finished():
    act = jav3.AgentTree([node(1, "waits", status="needs_you", needs="why",
                               started_at="2026-09-30 05:00:00"),
                          node(2, "old run", started_at="2026-09-30 03:00:00"),
                          node(3, "new run", started_at="2026-09-30 04:00:00")])
    fin = jav3.AgentTree([node(i, f"done {i}", status="done", started_at=f"2026-09-2{i} 01:00:00",
                               ended_at=f"2026-09-2{i} 02:00:00") for i in range(1, 9)]
                         + [node(3, "was running", status="done")])       # not under both
    run, done = jav3.agent_drawer_rows(act, fin, recent=3)
    assert [r["id"] for r in run] == [3, 2, 1]
    assert [r["id"] for r in done] == [8, 7, 6]


def test_session_sections_put_active_and_needs_input_above_the_rest():
    convs = [conv(5, "idle new"), conv(4, "web turn", running=True), conv(3, "asks"),
             conv(2, "agent waits"), conv(1, "idle old")]
    tree = jav3.AgentTree([node(2, "agent waits", kind="chat"),
                           node(20, "sub", parent=2, status="needs_you", needs="approve")])
    secs = jav3.session_sections(convs, tree, asks={3}, limit=5)
    assert [(n, [cv["id"] for cv, _, _ in rows]) for n, rows in secs] == \
        [("ACTIVE", [4]), ("NEEDS INPUT", [3, 2]), ("RECENT", [5, 1])]
    assert dict((cv["id"], why) for cv, _, why in secs[1][1])[2] == "approve"
    # the recent list is a handful: the oldest are left to /sessions
    many = [conv(i, f"c{i}") for i in range(30, 0, -1)]
    assert len(jav3.session_sections(many, jav3.AgentTree([]))[0][1]) == jav3.DRAWER_SESSIONS


# -- opening and closing ----------------------------------------------------------------------

async def test_left_and_right_open_the_drawers_on_an_empty_prompt_and_esc_closes():
    srv, app = make(seeded())
    async with app.run_test(size=(140, 40)) as pilot:
        d = await ready(pilot, app, "agents")
        assert d.kind == "agents" and app.focused is d and app.focus_no == 1
        assert under(app, "RUNNING", "Ship the login page")
        assert under(app, "RUNNING", "fix the invoice export")     # started in the web: no key
        assert under(app, "RECENT", "Fetch the morning quotes")
        await pilot.press("escape")
        assert await wait_for(lambda: app.drawer is None and app.focused is app.editor)
        d = await ready(pilot, app, "sessions", n=2)
        assert d.kind == "sessions" and app.focused is d
        assert under(app, "ACTIVE", "fix the invoice export")
        await pilot.press("escape")
        assert await wait_for(lambda: app.drawer is None and app.focused is app.editor)
        # the arrow back in the other direction puts each away too (← / → twice)
        await pilot.press("left")
        assert await wait_for(lambda: app.drawer is not None)
        await pilot.press("left")                      # at the top of the list: closes
        assert await wait_for(lambda: app.drawer is None)
        await pilot.press("right")
        assert await wait_for(lambda: app.drawer is not None)
        await pilot.press("right")
        assert await wait_for(lambda: app.drawer is None)
        assert app.focused is app.editor


async def test_the_arrows_move_the_cursor_when_the_prompt_has_text():
    srv, app = make(seeded())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.editor.text = "abc"
        app.editor.move_cursor((0, 3))
        await pilot.press("left")
        assert app.editor.cursor_location == (0, 2) and app.drawer is None
        await pilot.press("right", "right")
        assert app.editor.cursor_location == (0, 3) and app.drawer is None
        await pilot.pause(0.2)
        assert app.drawer is None and app.focused is app.editor
        app.editor.text = ""
        await pilot.press("left")
        assert await wait_for(lambda: app.drawer is not None)


async def test_a_page_keeps_its_own_arrows_and_the_page_commands_stay():
    srv, app = make(seeded())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/agents")
        assert await wait_for(lambda: type(app.top).__name__ == "AgentsPage")
        await pilot.press("right", "left")
        await pilot.pause(0.2)
        assert app.drawer is None                      # the page's own ← →
        app.dispatch("/work")
        assert await wait_for(lambda: type(app.top).__name__ == "ChatPage")
        app.dispatch("/sessions 7")                    # the picker's command form still opens it
        assert await wait_for(lambda: app.cid == 7)


async def test_the_drawer_leaves_the_focused_panel_and_a_streaming_turn_alone():
    srv, app = make(seeded())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "go")
        assert await wait_for(lambda: srv.feeds)
        srv.feed.put({"type": "start", "conversation_id": 4})
        assert await wait_for(lambda: app.busy and app.cid == 4)
        await pilot.press("left")
        assert await wait_for(lambda: app.drawer is not None and app.drawer.rows)
        assert app.busy and app.cid == 4 and app.focus_no == 1 and not srv.stops
        srv.feed.put({"type": "token", "text": "still streaming"})
        assert await wait_for(lambda: any("still streaming" in r.source
                                          for r in app.query("Reply")))
        await pilot.press("down", "down", "escape")
        assert await wait_for(lambda: app.drawer is None)
        assert app.busy and app.cid == 4 and not srv.stops and len(srv.feeds) == 1
        srv.feed.put({"type": "final", "content": "done", "conversation_id": 4})
        srv.feed.close()
        assert await wait_for(lambda: not app.busy)


# -- moving, and into the sub-agents -----------------------------------------------------------

async def test_up_down_move_and_right_left_step_into_and_out_of_an_entrys_sub_agents():
    srv, app = make(seeded())
    async with app.run_test(size=(140, 40)) as pilot:
        d = await ready(pilot, app, "agents")
        assert sel(app) == 10 and d.open_root == 10          # the first entry, unfolded
        kids = [k for k, _, _ in drawer_rows(app)]
        assert kids.count("kid") == 2
        await pilot.press("right")
        assert d.in_kids and sel(app) == 12
        await pilot.press("down")
        assert sel(app) == 13
        assert any("!" in t and "Check the copy" in t for k, _, t in drawer_rows(app)
                   if k == "kid")                             # the one that waits is marked
        await pilot.press("down")                             # past the last: the next entry
        assert not d.in_kids and sel(app) == 41
        await pilot.press("up", "right")
        assert d.in_kids and sel(app) == 12
        await pilot.press("left")
        assert not d.in_kids and sel(app) == 10 and app.drawer is d    # left out, not closed
        await pilot.press("down", "down")
        assert sel(app) == 5                                  # on into RECENT
        await pilot.press("k", "j", "j")
        assert sel(app) == 4
        await pilot.press("pageup")                           # the chat's key, not the drawer's
        assert app.drawer is d


async def test_a_running_entry_with_a_child_that_waits_is_marked_and_listed_with_the_others():
    srv, app = make(seeded())
    async with app.run_test(size=(140, 40)) as pilot:
        await ready(pilot, app, "agents")
        row = next(t for k, key, t in drawer_rows(app) if key == 10)
        assert "!" in row and "Ship the login page" in row and "▾" in row
        # the width is the drawer's: every row fits inside it
        assert all(len(t) <= app.drawer.row_width + 4 for _, _, t in drawer_rows(app))


# -- enter and p -------------------------------------------------------------------------------

async def test_enter_opens_the_entry_in_the_focused_panel_and_closes_the_drawer():
    srv, app = make(seeded())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await split(pilot, app, 2)                      # 1 | 2, panel 2 focused
        assert app.focus_no == 2
        await pilot.press("left")
        assert await wait_for(lambda: app.drawer is not None and app.drawer.fetched
                              and len(app.drawer.rows) > 4)
        await pilot.press("down", "down")                   # 10 -> 41 -> RECENT 5
        assert sel(app) == 5
        await pilot.press("enter")
        assert await wait_for(lambda: app.chats[2].cid == 5 and app.drawer is None)
        assert app.chats[1].cid is None and app.focus_no == 2 and len(app.chats) == 2
        assert any("chat 5" in str(w.render()) for w in app.chats[2].log.query("UserMsg"))
        assert await wait_for(lambda: app.focused is app.editor)
        # panel 1 the same way: focus it, open a session from the → drawer
        app.focus_panel(1)
        await ready(pilot, app, "sessions", n=2)
        await pilot.press("down", "down")
        assert sel(app) == 8
        await pilot.press("enter")
        assert await wait_for(lambda: app.chats[1].cid == 8 and app.drawer is None)
        assert app.chats[2].cid == 5 and app.focus_no == 1


async def test_enter_on_a_running_web_chat_attaches_to_its_live_stream():
    srv = seeded()
    srv.conv_messages[41] = {"messages": [{"id": 1, "role": "user", "content": "fix it",
                                           "created_at": "t0"}], "running": True,
                             "pending_activity": [], "agent_slug": None}
    srv, app = make(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await ready(pilot, app, "sessions", n=2)
        assert under(app, "ACTIVE", "fix the invoice export") and sel(app) == 41
        await pilot.press("enter")
        assert await wait_for(lambda: app.cid == 41 and srv.streams.get(41))
        srv.streams[41][-1].put({"type": "final", "content": "done", "conversation_id": 41})
        srv.streams[41][-1].close()
        assert await wait_for(lambda: not app.busy)


async def test_p_opens_the_entry_in_a_new_panel_beside_the_focused_one():
    srv, app = make(seeded())
    async with app.run_test(size=(140, 40)) as pilot:
        await ready(pilot, app, "agents")
        await pilot.press("down", "down")                  # 10 -> 41 -> RECENT: 5
        assert sel(app) == 5
        await pilot.press("p")
        assert await wait_for(lambda: len(app.chats) == 2 and app.chats[2].cid == 5)
        assert app.drawer is None and app.focus_no == 2 and app.chats[1].cid is None
        # the same from the sessions drawer
        await ready(pilot, app, "sessions", n=2)
        await pilot.press("down", "p")
        assert await wait_for(lambda: len(app.chats) == 3 and app.chats[3].cid == 7)
        assert app.focus_no == 3 and app.drawer is None


async def test_p_is_refused_at_four_panels_with_a_note_and_the_drawer_stays():
    srv, app = make(seeded())
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause(0.3)
        for want in (2, 3, 4):
            await split(pilot, app, want)
        await pilot.press("left")
        assert await wait_for(lambda: app.drawer is not None and len(app.drawer.rows) > 4)
        before = {n: c.cid for n, c in app.chats.items()}
        await pilot.press("p")
        assert await wait_for(lambda: "4 panels is the most" in str(
            app.drawer.query_one("#dr-msg").render()))
        assert app.drawer is not None and len(app.chats) == 4
        assert {n: c.cid for n, c in app.chats.items()} == before
        # enter still opens it in the focused panel
        await pilot.press("down", "down", "enter")
        assert await wait_for(lambda: app.drawer is None and app.chats[app.focus_no].cid == 5)


# -- width and place ---------------------------------------------------------------------------

async def test_the_drawer_slides_from_its_own_edge_and_leaves_a_panel_visible_at_80x24():
    srv, app = make(seeded())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)
        await split(pilot, app, 2)
        d = await ready(pilot, app, "agents")
        area = app.panel_area.size
        assert d.region.x == 0 and d.region.width == jav3.drawer_width(80) == 36
        assert d.region.height == area.height
        assert area.width - d.region.width >= jav3.PANEL_MIN_COLS    # a panel's worth stays
        await pilot.press("escape")
        d = await ready(pilot, app, "sessions", n=2)
        assert d.region.right == area.width and d.region.width == 36
    srv, app = make(seeded())
    async with app.run_test(size=(160, 48)) as pilot:
        d = await ready(pilot, app, "agents")
        assert d.region.width == 40 and d.region.x == 0


# -- live ---------------------------------------------------------------------------------------

async def test_a_chat_started_in_the_web_appears_in_running_without_a_key():
    srv, app = make(DrawerServer())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("left")
        assert await wait_for(lambda: app.drawer is not None and app.drawer.fetched)
        assert has(app, "nothing is running") and not has(app, "late chat")
        assert app.drawer.RELOAD_S <= 2
        srv.convs = [conv(50, "late chat")]
        srv.running = [50]
        assert await wait_for(lambda: under(app, "RUNNING", "late chat"), tries=100, step=0.05)
        assert not has(app, "nothing is running")
        # and in the sessions drawer, under ACTIVE
        await pilot.press("escape")
        await pilot.press("right")
        assert await wait_for(lambda: app.drawer is not None and app.drawer.kind == "sessions"
                              and under(app, "ACTIVE", "late chat"), tries=100, step=0.05)


async def test_run_end_on_the_shared_stream_moves_a_finished_run_to_recent_at_once(monkeypatch):
    srv, app = make(seeded(full=True), token=FULL)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert await wait_for(lambda: srv.events)
        assert srv.event_paths[0] == "topics=notices,security,runs"
        monkeypatch.setattr(app.Drawer, "RELOAD_S", 600.0)    # no polling: only run_end reloads
        await pilot.press("left")
        assert await wait_for(lambda: app.drawer is not None and app.drawer.fetched
                              and under(app, "RUNNING", "fix the invoice export"))
        await pilot.pause(0.2)
        # the web chat's turn ends: the server now lists it as finished
        srv.running = []
        srv.active = [n for n in srv.active if n["id"] != 41]
        srv.finished = [node(41, "fix the invoice export", kind="chat", status="done",
                             started_at="2026-09-30 05:00:00",
                             ended_at="2026-09-30 05:20:00")] + srv.finished
        reloads = len(srv.queries)
        assert under(app, "RUNNING", "fix the invoice export")       # not yet: nobody asked
        srv.say({"type": "run_end", "conversation_id": 41, "kind": "chat"})
        assert await wait_for(lambda: under(app, "RECENT", "fix the invoice export"),
                              tries=60, step=0.05)                 # 3 s at most, not 600
        assert len(srv.queries) > reloads
        assert not under(app, "RUNNING", "fix the invoice export")


async def test_a_server_without_the_runs_topic_still_gets_the_other_two():
    srv = seeded(full=True)
    srv.event_status = 400
    srv, app = make(srv, token=FULL)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await wait_for(lambda: len(srv.event_paths) >= 2 and srv.events)
        assert srv.event_paths[:2] == ["topics=notices,security,runs", "topics=notices,security"]
        srv.say({"type": "run_end", "conversation_id": 41}, topic="runs")   # nothing open: harmless
        await pilot.pause(0.2)


async def test_the_sessions_drawer_orders_active_and_needs_input_before_the_rest():
    srv = seeded()
    srv.convs = [conv(9, "newest idle", started_at="2026-09-30 09:00:00"),
                 conv(41, "web turn", started_at="2026-09-30 05:00:00"),
                 conv(11, "blocked chat", started_at="2026-09-30 04:00:00"),
                 conv(7, "old chat", started_at="2026-09-29 05:00:00")]
    srv.active += [node(11, "blocked chat", kind="chat", status="needs_you",
                        needs="waiting on your answer: run it?")]
    srv, app = make(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        await ready(pilot, app, "sessions", n=4)
        order = [(k, key) for k, key, _ in drawer_rows(app) if k in ("section", "chat")]
        names = [t.split("  ")[0].strip() for k, _, t in drawer_rows(app) if k == "section"]
        assert names == ["ACTIVE", "NEEDS INPUT", "RECENT"]
        assert [key for k, key in order if k == "chat"] == [41, 11, 9, 7]
        text = {key: t for k, key, t in drawer_rows(app) if k == "chat"}
        assert "!" in text[11] and "!" not in text[9] and "web turn" in text[41]
        assert sel(app) == 41                                       # the first one to look at


async def test_switching_drawers_and_slash_leave_the_focused_panel_alone():
    srv, app = make(seeded())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await split(pilot, app, 2)
        app.focus_panel(1)
        await ready(pilot, app, "agents")
        app.action_sessions_view()                 # one drawer at a time: this one gives way
        assert await wait_for(lambda: app.drawer is not None and app.drawer.kind == "sessions"
                              and app.drawer.rows and app.focused is app.drawer)
        assert app.focus_no == 1 and len(app.query("Drawer")) == 1
        await pilot.press("slash")                 # on to a command: the prompt, "/" typed
        assert await wait_for(lambda: app.drawer is None and app.editor.text == "/")
        assert app.focused is app.editor and app.focus_no == 1
        app.editor.text = ""
        # a click on the prompt (the editor has the keyboard again): a key closes the drawer
        await ready(pilot, app, "agents")
        app.editor.focus()
        await pilot.pause(0.1)
        await pilot.press("x")
        assert await wait_for(lambda: app.drawer is None and app.editor.text == "x")
        assert app.focus_no == 1
