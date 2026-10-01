"""← in the terminal client lists everything running server-wide, not only what
this client started: a chat started in the web app, a plan item, a schedule run
and a sub-agent. GET /api/chat/agents?scope=active leaves out a plain chat whose
own turn is streaming (it is not agent work), so the screen merges in
GET /api/chat/running too, the way the web sidebar's Active group does.
JAV3_CLIENT=<path> runs this file against another copy of the client."""
import json

import httpx
import pytest

from cli_fake import FakeServer, load_client, wait_for

jav3 = load_client("jav3cli_agents_live")


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / "cfg" / "jav3"


def node(nid, title, parent=None, kind="agent", status="running", **kw):
    return {"id": nid, "parent_id": parent, "kind": kind, "title": title, "summary": title,
            "role": kw.pop("role", kind), "status": status, "needs": kw.pop("needs", None),
            "agent_slug": None, "project": kw.pop("project", "shop"), "model": None,
            "running": status in ("running", "needs_you"),
            "started_at": kw.pop("started_at", "2026-09-30 04:00:00"),
            "ended_at": kw.pop("ended_at", None), **kw}


def conv(cid, summary, **kw):
    return {"id": cid, "summary": summary, "title": kw.pop("title", None), "kind": "chat",
            "project_slug": kw.pop("project_slug", "shop"), "model": None,
            "started_at": kw.pop("started_at", "2026-09-30 04:30:00"), **kw}


class LiveServer(FakeServer):
    """FakeServer plus what ← reads: the agents tree, the finished page, the
    conversation list and /api/chat/running, all changeable while the app runs."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.active: list[dict] = []
        self.finished: list[dict] = []
        self.convs: list[dict] = []
        self.agent_queries: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        if path == "/api/chat/agents":
            self.calls.append((request.method, path))
            self.agent_queries.append(q)
            if q.get("scope") == "finished":
                roots = [n for n in self.finished if n["parent_id"] is None]
                return httpx.Response(200, json={"nodes": self.finished, "total": len(roots)})
            return httpx.Response(200, json={"nodes": self.active})
        if path == "/api/conversations" and request.method == "GET":
            self.calls.append((request.method, path))
            return httpx.Response(200, json={"conversations": self.convs})
        return super().handle(request)


async def boot(srv, **kw):
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport(), **kw)
    return app


async def open_agents(pilot, app):
    await pilot.pause(0.3)
    await pilot.press("left")
    assert await wait_for(lambda: type(app.screen).__name__ == "AgentsScreen")
    scr = app.screen
    assert await wait_for(lambda: scr.loaded)
    await pilot.pause(0.1)
    return scr


def rows(scr):
    return [(r.kind, r.nid, str(r.render())) for r in scr.query("AgentRow")]


def head(scr) -> str:
    return str(scr.query_one("#ag-head").render())


def titles(scr, *needles):
    """The text of each root row that mentions one of `needles`."""
    return [t for k, _, t in rows(scr) if k == "root" and any(n in t for n in needles)]


def section_of(scr, needle):
    """The section (Active / Needs you) the row that says `needle` is under."""
    cur = None
    for kind, _, text in rows(scr):
        if kind in ("section", "group"):
            cur = text.split("  ")[0]
        elif needle in text:
            return cur
    return None


# --- the data side, apart from the screen -----------------------------------------------

def test_running_chat_nodes_adds_only_what_the_tree_leaves_out():
    tree = [node(10, "Ship it", kind="orchestrator"), node(11, "[item i1] build", parent=10)]
    convs = {41: conv(41, "fix the invoice export"), 42: conv(42, "", title="Renamed")}
    extra, unknown = jav3.running_chat_nodes(tree, [10, 11, 41, 42, 99], convs)
    assert [n["id"] for n in extra] == [41, 42] and unknown == [99]
    web = extra[0]
    assert (web["kind"], web["status"], web["running"], web["parent_id"]) == \
        ("chat", "running", True, None)
    assert web["title"] == "fix the invoice export" and web["project"] == "shop"
    assert extra[1]["title"] == "Renamed"
    assert jav3.node_role(web) == "chat" and jav3.node_status(web) == "running"


def test_active_sections_order_running_before_needs_you():
    nodes = [node(1, "waits", status="needs_you", needs="approve", started_at="2026-09-30 05:00:00"),
             node(2, "old run", started_at="2026-09-30 03:00:00"),
             node(3, "new run", started_at="2026-09-30 04:00:00")]
    secs = jav3.agent_sections(jav3.AgentTree(nodes), "active")
    assert [(name, [r["id"] for r in rs]) for name, rs in secs] == \
        [("Active", [3, 2]), ("Needs you", [1])]


# --- ← on the screen --------------------------------------------------------------------

async def test_a_chat_started_in_the_web_app_shows_under_left():
    srv = LiveServer()
    srv.running = [41]
    srv.convs = [conv(41, "fix the invoice export", project_slug="shop"), conv(7, "old chat")]
    app = await boot(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        scr = await open_agents(pilot, app)
        assert await wait_for(lambda: titles(scr, "fix the invoice export"))
        assert section_of(scr, "fix the invoice export") == "Active"
        assert "old chat" not in " ".join(t for _, _, t in rows(scr))   # idle chats stay out
        assert await wait_for(lambda: "1 running" in head(scr))
        assert "⌂ shop" in titles(scr, "fix the invoice export")[0]


async def test_plan_item_schedule_and_sub_agent_all_show_with_the_web_chat():
    srv = LiveServer()
    srv.running = [41, 43]
    srv.convs = [conv(41, "refactor billing")]
    srv.active = [
        # a plan: orchestrator > head > running item, one finished item
        node(10, "Ship the login page", kind="orchestrator", project="site"),
        node(11, "Login page", parent=10, kind="head", role="plan", project="site"),
        node(12, "Build the form", parent=11, role="item i1", project="site"),
        node(13, "Write tests", parent=11, role="item i2", status="done", project="site",
             ended_at="2026-09-30 04:10:00"),
        # a schedule run: no parent, named agent
        node(20, "Fetch the morning quotes", role="@morning-stocks", project="home"),
        # the web chat spawned a sub-agent: the chat is the root, the sub-agent its child
        node(41, "refactor billing", kind="chat", role="chat"),
        node(43, "Audit the invoice code", parent=41, role="@auditor"),
    ]
    app = await boot(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        scr = await open_agents(pilot, app)
        assert await wait_for(lambda: len(titles(scr, "Ship the login", "Fetch the morning",
                                                  "refactor billing")) == 3)
        for needle in ("Ship the login page", "Fetch the morning quotes", "refactor billing"):
            assert section_of(scr, needle) == "Active", needle
        # the chat appears once, not once from the tree and again from /running
        assert len(titles(scr, "refactor billing")) == 1
        assert await wait_for(lambda: "3 running" in head(scr))
        # the plan unfolds to its item, the sub-agent hangs under its chat
        tr = scr.tree
        assert [k["id"] for k, _ in tr.descendants(10)] == [11, 12, 13]
        assert [k["id"] for k, _ in tr.descendants(41)] == [43]
        assert tr.root_status(10) == "running"


async def test_a_chat_started_after_the_screen_opened_appears_without_a_key():
    srv = LiveServer()
    app = await boot(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        scr = await open_agents(pilot, app)
        assert not titles(scr, "late chat")
        assert "nothing is running" in " ".join(t for _, _, t in rows(scr))
        assert scr.RELOAD_S <= 2
        srv.convs = [conv(50, "late chat")]
        srv.running = [50]
        # the screen reloads by itself; no key pressed
        assert await wait_for(lambda: titles(scr, "late chat"), tries=70, step=0.05)
        assert await wait_for(lambda: "1 running" in head(scr))
        assert "nothing is running" not in " ".join(t for _, _, t in rows(scr))


async def test_a_run_that_finishes_leaves_the_running_group_on_reload():
    srv = LiveServer()
    srv.running = [41, 20]
    srv.convs = [conv(41, "web chat")]
    srv.active = [node(20, "Fetch the morning quotes", role="@morning-stocks", project="home")]
    srv.finished = [node(5, "Old report", status="done", ended_at="2026-09-29 10:00:00",
                         started_at="2026-09-29 09:00:00")]
    app = await boot(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        scr = await open_agents(pilot, app)
        assert await wait_for(lambda: len(titles(scr, "web chat", "Fetch the morning")) == 2)
        assert "Finished" in rows(scr)[-1][2] and " 1" in rows(scr)[-1][2]
        # the schedule run ends (now a finished root) and the web chat's turn ends
        srv.finished = [node(20, "Fetch the morning quotes", status="done", role="@morning-stocks",
                             started_at="2026-09-30 04:00:00", ended_at="2026-09-30 04:05:00"),
                        *srv.finished]
        srv.active, srv.running = [], []
        assert await wait_for(lambda: not titles(scr, "web chat", "Fetch the morning"),
                              tries=70, step=0.05)
        assert await wait_for(lambda: "0 running" in head(scr))
        assert await wait_for(lambda: "nothing is running" in " ".join(
            t for _, _, t in rows(scr)))
        # the count behind the Finished row follows, without pressing r
        assert await wait_for(lambda: " 2" in rows(scr)[-1][2], tries=70, step=0.05)
        # and opening it lists the full finished page, not the one-row count fetch
        await pilot.press("down", "enter")
        assert await wait_for(lambda: scr.mode == "finished")
        assert await wait_for(lambda: len(titles(scr, "Fetch the morning", "Old report")) == 2)


async def test_enter_opens_a_running_web_chat_and_follows_its_stream():
    srv = LiveServer()
    srv.running = [41]
    srv.convs = [conv(41, "fix the invoice export")]
    srv.conv_messages[41] = {"messages": [
        {"id": 1, "role": "user", "content": "fix the export", "created_at": "t0"}],
        "running": True, "pending_activity": [], "agent_slug": None}
    app = await boot(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        scr = await open_agents(pilot, app)
        assert await wait_for(lambda: titles(scr, "fix the invoice export"))
        assert scr.sel == 41
        await pilot.press("enter")
        assert await wait_for(lambda: type(app.screen).__name__ != "AgentsScreen")
        assert await wait_for(lambda: app.cid == 41)
        assert await wait_for(lambda: srv.streams.get(41))      # attached to the live turn
        srv.streams[41][-1].put({"type": "final", "content": "done", "conversation_id": 41})
        srv.streams[41][-1].close()
        assert await wait_for(lambda: not app.busy)


async def test_the_chat_you_came_from_is_selected_and_marked():
    srv = LiveServer()
    srv.running = [41, 20]
    srv.convs = [conv(41, "refactor billing", started_at="2026-09-30 04:00:00")]
    srv.active = [node(20, "Fetch the morning quotes", role="@morning-stocks", project="home",
                       started_at="2026-09-30 05:00:00")]
    srv.conv_messages[41] = {"messages": [
        {"id": 1, "role": "user", "content": "refactor billing", "created_at": "t0"}],
        "running": True, "pending_activity": [], "agent_slug": None}
    app = await boot(srv, resume=41)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await wait_for(lambda: app.cid == 41)
        await pilot.pause(0.3)
        app.action_agents_view()
        assert await wait_for(lambda: type(app.screen).__name__ == "AgentsScreen")
        scr = app.screen
        assert await wait_for(lambda: scr.loaded and titles(scr, "refactor billing"))
        assert scr.sel == 41
        assert [r.nid for r in scr.query("AgentRow") if r.has_class("current")] == [41]
        # a newer run sits above it, but the current one is the highlighted entry
        order = [nid for k, nid, _ in rows(scr) if k == "root"]
        assert order == [20, 41]
        await pilot.press("escape")
        assert await wait_for(lambda: type(app.screen).__name__ != "AgentsScreen")
        # the resumed chat is attached to its live turn: end it so the app can close
        assert await wait_for(lambda: srv.streams.get(41))
        srv.streams[41][-1].put({"type": "final", "content": "ok", "conversation_id": 41})
        srv.streams[41][-1].close()
        assert await wait_for(lambda: not app.busy)


async def test_a_running_chat_nobody_can_name_does_not_refetch_the_list_every_reload():
    """An incognito chat runs but is not in /api/conversations: it stays off the
    screen, and the list is not asked for again on every 1.5 s reload."""
    srv = LiveServer()
    srv.running = [90]
    srv.convs = [conv(7, "old chat")]
    app = await boot(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        scr = await open_agents(pilot, app)
        before = srv.calls.count(("GET", "/api/conversations"))
        reloads = len(srv.agent_queries)
        assert await wait_for(lambda: len(srv.agent_queries) >= reloads + 3, tries=140)
        assert srv.calls.count(("GET", "/api/conversations")) - before <= 1
        assert not titles(scr, "chat")
        # it is named once the server lists it: that is looked up again after a while
        srv.convs = [conv(90, "now listed"), conv(7, "old chat")]
        scr.feed._tried.clear()                       # the retry delay, skipped
        assert await wait_for(lambda: titles(scr, "now listed"), tries=70)


async def test_servers_without_running_or_conversations_routes_still_list_the_tree():
    """An older server answers 404 to /api/chat/running: the tree is all there is."""
    class Old(LiveServer):
        def handle(self, request):
            if request.url.path in ("/api/chat/running",):
                return httpx.Response(404, json={"detail": "nope"})
            return super().handle(request)
    srv = Old()
    srv.active = [node(20, "Fetch the morning quotes", role="@morning-stocks", project="home")]
    app = await boot(srv)
    async with app.run_test(size=(140, 40)) as pilot:
        scr = await open_agents(pilot, app)
        assert await wait_for(lambda: titles(scr, "Fetch the morning"))
        assert scr.error is None
        assert json.dumps(srv.agent_queries[0])           # the active query was made
