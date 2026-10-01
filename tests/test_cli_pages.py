"""jav3.2 P0: pages in the terminal client. A page is a widget a PageHost mounts (not a
pushed Screen) with a back stack, a registry names it (`/security calls`, `/vms images`,
`/agents finished`, `/work`), one place parses `[args] [panel]`, and the pages the web
has but the terminal does not answer where they live. The prompt stays under every page
as one line. The client file is loaded from tests/cli_fake.py."""
import httpx
import pytest

from cli_fake import load_client, pin_zone

jav3 = load_client("jav3cli_pages")

SESSION = "session:sess"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    yield from pin_zone(monkeypatch)


def _srv(seen=None, convs=None):
    """A logged-in operator's server where every read is empty, chats can be opened
    (`convs`: id -> messages) and a posted message is recorded and answered."""
    convs = convs or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if seen is not None:
            seen.append((method, path, request.content.decode() if request.content else ""))
        if "jarvis_token=sess" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"detail": "not authenticated"})
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "operator"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={"default": "deepseek/deepseek-flash",
                                             "models": [], "projects": [], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        if path.startswith("/api/conversations/") and path.endswith("/messages"):
            cid = int(path.split("/")[3])
            if cid in convs:
                return httpx.Response(200, json={"messages": convs[cid], "running": False})
        if path in ("/api/agents/notices/stream", "/api/events"):
            return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})
        if path == "/api/chat" and method == "POST":
            return httpx.Response(
                200, text='data: {"type": "final", "content": "ok", "conversation_id": 9}\n\n',
                headers={"content-type": "text/event-stream"})
        if path.startswith("/api/"):
            return httpx.Response(200, json={
                "projects": [], "pending": [], "events": [], "services": [], "packages": [],
                "secrets": [], "profiles": [], "boxes": [], "rows": [], "rules": [],
                "nodes": [], "total": 0})
        return httpx.Response(404, json={"detail": "nope"})
    return httpx.MockTransport(handler)


async def _until(pilot, cond, tries=80):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


def _name(app) -> str:
    return type(app.top).__name__


async def _go(pilot, app, line: str, name: str):
    """Run a slash command and wait for the page it opens."""
    app.dispatch(line)
    assert await _until(pilot, lambda: _name(app) == name), (line, _name(app))
    return app.top


def _notes(app) -> str:
    return " ".join(str(w.render()) for w in app.query("Note"))


# -- the parser and the registry (no terminal needed) -------------------------------------

def test_parse_page_args_splits_args_from_a_trailing_panel():
    p = jav3.parse_page_args
    assert p("") == ([], False) and p(None) == ([], False)
    assert p("calls") == (["calls"], False)
    assert p("calls panel") == (["calls"], True)
    assert p("panel") == ([], True)
    assert p("  finished   PANEL ") == (["finished"], True)
    assert p("a b panel") == (["a", "b"], True)
    assert p("panel calls") == (["panel", "calls"], False)      # only the last word counts
    assert p("panelx") == (["panelx"], False)


def test_the_registry_names_every_page_the_web_has():
    names = [s.name for s in jav3.PAGE_SPECS]
    assert names[:5] == ["work", "agents", "security", "vms", "sessions"]
    assert {"memory", "settings", "logs", "schedules", "tools", "artifacts"} <= set(names)
    assert jav3.WEB_PAGES == ("memory", "settings", "logs", "schedules", "tools", "artifacts")
    assert all(jav3.PAGES[n].kind == "web" for n in jav3.WEB_PAGES)
    assert jav3.PAGES["security"].hint == "[" + "|".join(jav3.SECURITY_TABS) + "]"
    assert jav3.PAGES["vms"].hint == "[boxes|images|catalogue]"
    assert jav3.PAGES["agents"].hint == "[finished]"
    assert jav3.PAGES["sessions"].hint == "[id]"
    msg = jav3.not_in_terminal("memory")
    assert "not in the terminal yet" in msg and "/web memory" in msg


def test_the_commands_table_carries_the_pages_with_their_hints():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    cmds = app.commands
    for name in ("work", "agents", "security", "vms", "sessions", "memory", "settings",
                 "logs", "schedules", "tools", "artifacts"):
        assert cmds[name].page == name, name
    assert cmds["security"].usage == jav3.PAGES["security"].hint
    assert cmds["vms"].usage == "[boxes|images|catalogue]"
    assert cmds["agents"].usage == "[finished]" and cmds["sessions"].usage == "[id]"
    # the old spellings are aliases of the same command
    assert cmds["review"] is cmds["security"] and cmds["agents-view"] is cmds["agents"]
    assert cmds["history"] is cmds["sessions"] and cmds["chat"] is cmds["work"]
    # leader keys are kept
    assert (cmds["agents"].leader, cmds["security"].leader, cmds["vms"].leader,
            cmds["sessions"].leader) == ("g", "v", "c", "l")
    # the argument menus: every tab, finished, `calls` and `rules` included
    assert [v for v, _ in app.arg_options("security")] == list(jav3.SECURITY_TABS)
    assert [v for v, _ in app.arg_options("vms")] == ["boxes", "images", "catalogue"]
    assert [v for v, _ in app.arg_options("agents")] == ["finished"]
    assert [v for v, _ in app.arg_options("web")] == list(jav3.WEB_PAGES)


# -- opening, replacing, going back --------------------------------------------------------

async def test_a_page_opens_by_command_with_its_args_and_esc_goes_back_to_the_chat():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        assert _name(app) == "ChatPage" and not app.paged
        page = await _go(pilot, app, "/security calls", "SecurityPage")
        assert page.args == ["calls"] and page.tab == "calls" and app.paged
        assert page.page_title == "Security · Calls" and page.page_visible
        assert app.focused is page                                  # its keys work at once
        assert app.query_one("#bottom").has_class("paged")
        await pilot.press("escape")
        assert await _until(pilot, lambda: _name(app) == "ChatPage" and app.focused is app.editor)
        assert not app.query_one("#bottom").has_class("paged")
        # the same with /vms images and /agents finished
        page = await _go(pilot, app, "/vms images", "VmsPage")
        assert page.tab == "images" and page.args == ["images"]
        await pilot.press("escape")
        assert await _until(pilot, lambda: _name(app) == "ChatPage")
        page = await _go(pilot, app, "/agents finished", "AgentsPage")
        assert page.mode == "finished" and page.page_title == "Agents · finished"


async def test_pages_stack_esc_walks_back_and_ctrl_c_goes_straight_to_the_chat():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        sec = await _go(pilot, app, "/security", "SecurityPage")
        vms = await _go(pilot, app, "/vms", "VmsPage")
        assert [type(p).__name__ for p in app.host.stack] == ["ChatPage", "SecurityPage",
                                                              "VmsPage"]
        assert vms.page_visible and not sec.page_visible and not sec.display
        await pilot.press("escape")                                  # back to /security
        assert await _until(pilot, lambda: app.page is sec)
        assert sec.page_visible and sec.display and app.focused is sec
        await pilot.press("escape")
        assert await _until(pilot, lambda: _name(app) == "ChatPage")
        # ctrl+c leaves a page for the chat whatever is stacked under it
        await _go(pilot, app, "/security", "SecurityPage")
        await _go(pilot, app, "/vms", "VmsPage")
        await pilot.press("ctrl+c")
        assert await _until(pilot, lambda: _name(app) == "ChatPage")
        assert len(app.host.stack) == 1


async def test_opening_a_page_that_is_showing_gives_it_the_new_args_in_place():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        sec = await _go(pilot, app, "/security", "SecurityPage")
        assert sec.tab == "queue"
        app.dispatch("/security network")
        assert await _until(pilot, lambda: sec.tab == "network")
        assert app.page is sec and len(app.host.stack) == 2          # no second page
        app.dispatch("/security nonsense")                            # an unknown tab: the first
        assert await _until(pilot, lambda: sec.tab == "queue")
        # buried under another page, it is opened fresh on top
        await _go(pilot, app, "/vms", "VmsPage")
        again = await _go(pilot, app, "/security calls", "SecurityPage")
        assert again is not sec and again.tab == "calls"
        assert [type(p).__name__ for p in app.host.stack] == ["ChatPage", "VmsPage",
                                                              "SecurityPage"]
        # /agents is the same for its two views
        ag = await _go(pilot, app, "/agents", "AgentsPage")
        assert ag.mode == "active"
        app.dispatch("/agents finished")
        assert await _until(pilot, lambda: ag.mode == "finished")
        app.dispatch("/agents")
        assert await _until(pilot, lambda: ag.mode == "active")


async def test_work_is_the_chat_and_left_on_an_empty_prompt_opens_the_agents_page():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("left")
        assert await _until(pilot, lambda: _name(app) == "AgentsPage")
        await _go(pilot, app, "/vms", "VmsPage")
        app.dispatch("/work")
        assert await _until(pilot, lambda: _name(app) == "ChatPage")
        assert len(app.host.stack) == 1
        assert await _until(pilot, lambda: app.focused is app.editor)
        app.dispatch("/work")                                        # already there: nothing
        await pilot.pause(0.2)
        assert _name(app) == "ChatPage"


async def test_agents_with_a_slug_is_still_the_persona_picker():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/agents frontend")
        assert await _until(pilot, lambda: app.agent == "frontend")
        assert _name(app) == "ChatPage"


# -- `panel`, and the pages the terminal does not have -------------------------------------

async def test_a_trailing_panel_opens_in_place_with_a_one_line_note():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        page = await _go(pilot, app, "/vms images panel", "VmsPage")
        assert page.args == ["images"]                               # `panel` is not an arg
        assert await _until(pilot, lambda: "panels arrive in the next step" in _notes(app))
        # a page that is not the chat: the note is also a toast, so it is seen
        app.dispatch("/security panel")
        assert await _until(pilot, lambda: _name(app) == "SecurityPage")
        assert app.page.tab == "queue"


async def test_web_only_pages_say_where_they_live_and_leave_the_view_alone(monkeypatch):
    pytest.importorskip("textual")
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        for name in jav3.WEB_PAGES:
            app.dispatch(f"/{name}")
            assert await _until(pilot, lambda n=name: f"/web {n} opens it" in _notes(app)), name
        assert "not in the terminal yet" in _notes(app)
        assert _name(app) == "ChatPage" and not app.paged
        assert "/details" in str(next(n.render() for n in app.query("Note")
                                      if "/tools" in str(n.render())))
        # the answer is true: /web <page> opens it
        app.dispatch("/web memory")
        assert await _until(pilot, lambda: opened == ["http://h:1/memory"])
        app.dispatch("/web")
        assert await _until(pilot, lambda: len(opened) == 2 and opened[1] == "http://h:1/")
        # over a page the note cannot be seen in the chat: it comes as a toast too
        await _go(pilot, app, "/vms", "VmsPage")
        app.dispatch("/memory")
        assert await _until(pilot, lambda: any("not in the terminal yet" in str(n.message)
                                               for n in app._notifications))
        assert _name(app) == "VmsPage"


# -- the prompt line over a page -----------------------------------------------------------

async def test_slash_on_a_page_types_a_command_and_esc_gives_the_page_its_keys_back():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        sec = await _go(pilot, app, "/security", "SecurityPage")
        await pilot.press("slash")
        assert await _until(pilot, lambda: app.focused is app.editor)
        assert app.editor.text == "/" and app.popup_open()           # the command menu
        await pilot.press("escape")                                  # one esc: menu, text, focus
        assert await _until(pilot, lambda: app.focused is sec)
        assert app.editor.text == "" and not app.popup_open() and _name(app) == "SecurityPage"
        await pilot.press("2")                                       # the page's own key again
        assert await _until(pilot, lambda: sec.tab == "network")
        # type a /page command there and it runs: the next page is on top
        await pilot.press("slash")
        await pilot.pause(0.1)
        for ch in "vms images":
            await pilot.press("space" if ch == " " else ch)
        await pilot.pause(0.2)
        await pilot.press("enter")
        assert await _until(pilot, lambda: _name(app) == "VmsPage")
        assert app.page.tab == "images" and app.focused is app.page


async def test_letters_typed_right_after_a_slash_reach_the_prompt_not_the_page():
    """Posted back to back (a fast typist, a paste of keys): the slash moves the focus
    before the next key is handed out, so `a` is not the Security page's acknowledge."""
    pytest.importorskip("textual")
    from textual import events
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        await _go(pilot, app, "/security", "SecurityPage")
        for ch in "/agents finished":
            app.post_message(events.Key("slash" if ch == "/" else "space" if ch == " "
                                        else ch, ch))
        assert await _until(pilot, lambda: app.editor.text == "/agents finished")
        app.post_message(events.Key("enter", None))
        assert await _until(pilot, lambda: _name(app) == "AgentsPage")
        assert app.page.mode == "finished"


async def test_plain_text_typed_on_the_prompt_over_a_page_goes_to_the_chat():
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv(seen))
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        await _go(pilot, app, "/vms", "VmsPage")
        app.command_line("hello there")
        await pilot.press("enter")
        assert await _until(pilot, lambda: _name(app) == "ChatPage")
        assert await _until(pilot, lambda: any(m == "POST" and p == "/api/chat"
                                               and "hello there" in b for m, p, b in seen))


async def test_the_status_row_and_prompt_follow_the_page_showing():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        chat_hint = app.editor.placeholder
        assert "enter send" in str(app.query_one("#status-left").render())
        await _go(pilot, app, "/vms catalogue", "VmsPage")
        assert await _until(pilot, lambda: "VMs · Catalogue"
                            in str(app.query_one("#status-left").render()))
        assert "esc back" in str(app.query_one("#status-left").render())
        assert app.editor.placeholder != chat_hint and "/work" in app.editor.placeholder
        assert not app.query_one("#meta").display                     # the chat's meta row
        await pilot.press("escape")
        assert await _until(pilot, lambda: app.editor.placeholder == chat_hint)
        assert app.query_one("#meta").display


async def test_chat_keys_are_the_chats_and_the_leader_works_on_a_page():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        assert app.check_action("scroll_log", ()) and app.check_action("ctrl_d", ())
        await _go(pilot, app, "/security", "SecurityPage")
        # pgup/pgdn, ctrl+o, ctrl+g and ctrl+d are the page's to use, not the chat's
        for act in ("scroll_log", "toggle_tools", "external_editor", "ctrl_d"):
            assert not app.check_action(act, ()), act
        assert app.check_action("leader", ()) and app.check_action("toggle_sidebar", ())
        # ctrl+x then c: /vms, over the security page (its own `c` key is not hit)
        await pilot.press("ctrl+x")
        await pilot.press("c")
        assert await _until(pilot, lambda: _name(app) == "VmsPage")
        assert [type(p).__name__ for p in app.host.stack][-2:] == ["SecurityPage", "VmsPage"]


async def test_sessions_with_an_id_from_a_page_shows_the_chat_and_opens_it():
    pytest.importorskip("textual")
    seen: list = []
    convs = {5: [{"id": 1, "role": "user", "content": "first question"},
                 {"id": 2, "role": "assistant", "content": "an answer", "model": "m"}]}
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv(seen, convs))
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        await _go(pilot, app, "/security", "SecurityPage")
        app.dispatch("/sessions 5")
        assert await _until(pilot, lambda: _name(app) == "ChatPage" and app.cid == 5)
        assert await _until(pilot, lambda: "first question" in
                            " ".join(str(w.render()) for w in app.query("UserMsg")))


async def test_a_dialog_is_still_modal_over_a_page():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        sec = await _go(pilot, app, "/security", "SecurityPage")
        app.dispatch("/help")
        assert await _until(pilot, lambda: _name(app) == "Help")
        assert app.page is sec and not sec.active                    # polling waits for it
        await pilot.press("escape")
        assert await _until(pilot, lambda: _name(app) == "SecurityPage")
        assert sec.active and app.focused is sec


async def test_the_old_tab_click_still_works_through_the_page():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        sec = await _go(pilot, app, "/security", "SecurityPage")
        await pilot.click("#sec-tab-logs")
        assert await _until(pilot, lambda: sec.tab == "logs")


# -- what P1 builds on: a page does not own the terminal -----------------------------------

async def test_a_page_hides_and_shows_with_its_polling_and_its_hooks():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    Page = type(app).Page
    log: list = []

    class Probe(Page):
        NAME = "probe"

        def on_mount(self) -> None:
            self.poll(0.05, lambda: log.append("tick"))

        def page_shown(self) -> None:
            log.append(f"shown{self.shown_count}")

        def page_hidden(self) -> None:
            log.append("hidden")

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        probe = Probe(["a", "b"])
        await app.host.push(probe)
        assert probe.args == ["a", "b"] and app.page is probe
        assert await _until(pilot, lambda: app.focused is probe)
        assert await _until(pilot, lambda: log.count("tick") >= 2)
        assert log[0] == "shown1"
        await _go(pilot, app, "/vms", "VmsPage")                      # something over it
        assert not probe.page_visible and log[-1] in ("hidden", "tick")
        await pilot.pause(0.1)
        frozen = log.count("tick")
        await pilot.pause(0.4)
        assert log.count("tick") == frozen                            # paused while hidden
        await pilot.press("escape")
        assert await _until(pilot, lambda: app.page is probe)
        assert await _until(pilot, lambda: log.count("tick") > frozen)
        assert "shown2" in log and "hidden" in log
        probe.leave()                                                 # a page leaves by asking
        assert await _until(pilot, lambda: _name(app) == "ChatPage")
        assert not probe.is_attached


async def test_a_page_sizes_itself_from_its_host_and_leaves_through_it_not_the_app():
    pytest.importorskip("textual")
    from textual.containers import Vertical
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    Page, PageHost = type(app).Page, type(app).PageHost

    class Base(Page, can_focus=False):
        NAME = "base"

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        # a second host, a quarter of the screen: the shape a panel will have
        box = Vertical(id="panel")
        box.styles.layer = "above"
        box.styles.dock = "right"
        box.styles.width, box.styles.height = 50, 18
        host = PageHost(Base(), id="host2")
        await app.screen.mount(box)
        await box.mount(host)
        await pilot.pause(0.2)
        sec = app.page_classes["security"](["calls"])
        await host.push(sec)
        await pilot.pause(0.3)
        assert sec.size.width <= 50 and sec.size.height <= 18 and sec.size.width > 20
        assert host.current is sec and app.page is not sec           # the app's host is its own
        assert len(app.host.stack) == 1
        # esc inside it pops that host's stack only
        sec.focus()
        await pilot.press("escape")
        assert await _until(pilot, lambda: host.current is not sec)
        assert len(app.host.stack) == 1 and _name(app) == "ChatPage"
        # a narrow page still draws: the calls rows and tabs take the compact forms
        await host.push(app.page_classes["vms"]([]))
        await pilot.pause(0.3)
        assert host.current.size.width <= 50
