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
