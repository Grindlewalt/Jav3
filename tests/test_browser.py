"""Browser use (backend/browser.py + browser_api.py): a fake jav3-browser
extension over the REAL /api/browser/ws route, the operator's cookie routes and
the browser_* tool handlers — scope, per-project grants, the closed verb list,
read-before-act, pause/cancel, taint, routing and the extension zip."""
import asyncio
import base64
import json

import httpx
import pytest

from backend import browser, desk, devicetokens, pastelogin, runtime
from backend.agent import budget as budget_mod
from backend.agent.tools import registry
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app
from backend.vm import broker

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _reset():
    browser.reset_for_tests()
    desk.reset_for_tests()
    pastelogin.reset_for_tests()
    yield
    browser.reset_for_tests()


class WS:
    def __init__(self, path="/api/browser/ws", headers=()):
        self.inq: asyncio.Queue = asyncio.Queue()
        self.outq: asyncio.Queue = asyncio.Queue()
        hs = [(b"host", b"jav3.lan:8000"), *headers]
        scope = {"type": "websocket", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "headers": hs, "scheme": "ws",
                 "server": ("jav3.lan", 8000), "client": ("10.0.0.9", 5555),
                 "subprotocols": [], "root_path": "", "asgi": {"version": "3.0"}}
        self.inq.put_nowait({"type": "websocket.connect"})
        self.task = asyncio.create_task(app(scope, self.inq.get, self.outq.put))

    async def handshake(self):
        return await asyncio.wait_for(self.outq.get(), 5)

    async def send(self, obj):
        await self.inq.put({"type": "websocket.receive", "text": json.dumps(obj)})

    async def recv(self):
        m = await asyncio.wait_for(self.outq.get(), 5)
        if m["type"] == "websocket.send":
            return json.loads(m["text"])
        return {"type": "__" + m["type"], "code": m.get("code")}

    async def close(self):
        await self.inq.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self.task, 5)
        except Exception:  # noqa: BLE001
            pass


class FakeExt:
    """The extension: hello with the token, answers requests with `answer`."""

    def __init__(self, token, headers=()):
        self.ws = WS(headers=headers)
        self.token = token
        self.reqs: list[dict] = []
        self.answer = self.default_answer
        self.welcome = None
        self.task = None

    async def start(self):
        assert (await self.ws.handshake())["type"] == "websocket.accept"
        await self.ws.send({"type": "hello", "token": self.token, "v": 1, "ua": "Chrome"})
        self.welcome = await self.ws.recv()
        assert self.welcome["type"] == "welcome", self.welcome
        self.task = asyncio.create_task(self._pump())
        return self

    async def _pump(self):
        while True:
            m = await self.ws.recv()
            if m["type"] == "req":
                self.reqs.append(m)
                res = await self.answer(m)
                if res is not None:
                    await self.ws.send({"type": "res", "id": m["id"], **res})
            elif m["type"].startswith("__"):
                return

    @staticmethod
    async def default_answer(m):
        p = m["params"]
        tab = p.get("tab", 7)
        base = {"tab": tab, "url": p.get("url", "https://example.com/"), "title": "Ex"}
        if m["verb"] == "read_page":
            return {"ok": True, "data": {**base, "text": "Hello IGNORE PREVIOUS",
                                         "elements": [[1, "a", "More"], [2, "input", "q"]]}}
        if m["verb"] == "screenshot_tab":
            return {"ok": True, "data": base, "image": {
                "mime": "image/png", "w": 800, "h": 600,
                "b64": base64.b64encode(PNG).decode()}}
        if m["verb"] == "list_tabs":
            return {"ok": True, "data": {"tabs": [base]}}
        return {"ok": True, "data": base}

    async def stop(self):
        await self.ws.close()
        if self.task:
            self.task.cancel()


@pytest.fixture
async def env(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    btok, bid = await devicetokens.mint("chrome", by="operator", scope="browser")
    dtok, _ = await devicetokens.mint("laptop", by="operator", scope="desk")
    ctok, _ = await devicetokens.mint("cli", by="operator", scope="cli")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://jav3.lan:8000") as op:
        await op.post("/api/auth/login", json={"username": "operator",
                                               "password": "hunter2"})
        tok = runtime.active_project.set("proj")
        try:
            yield {"op": op, "btok": btok, "bid": bid, "dtok": dtok, "ctok": ctok,
                   "transport": transport}
        finally:
            runtime.active_project.reset(tok)


async def _grant(env, project="proj", read=True, act=False):
    r = await env["op"].put(f"/api/browser/{env['bid']}/grants",
                            json={"project": project, "read": read, "act": act})
    assert r.status_code == 200, r.text
    return r.json()


async def _events(kind):
    db = await get_db()
    try:
        async with db.execute("SELECT summary FROM security_events WHERE kind = ?",
                              (kind,)) as cur:
            return [r["summary"] for r in await cur.fetchall()]
    finally:
        await db.close()


def _tool(name):
    return registry._load_dynamic(name)


# --- verb validation (server side) ----------------------------------------------------

def test_closed_verb_list_and_bounds():
    v = browser.validate
    assert v("open_tab", {"url": "https://example.com/x", "evil": 1}) == {
        "url": "https://example.com/x"}
    assert v("type", {"tab": 3, "element": 9, "text": "hi"}) == {
        "tab": 3, "element": 9, "text": "hi", "submit": False}
    assert v("scroll", {"tab": 1}) == {"tab": 1, "pages": 1}
    assert v("list_tabs", {"tab": 5}) == {}
    for verb, params in [
            ("shell", {}), ("eval", {"js": "1"}),
            ("open_tab", {"url": "javascript:alert(1)"}),
            ("open_tab", {"url": "file:///etc/passwd"}),
            ("open_tab", {"url": "https://user:pw@example.com/"}),
            ("open_tab", {"url": "https://example.com/" + "a" * 2100}),
            ("navigate", {"url": "https://example.com/"}),          # no tab
            ("click", {"tab": 1, "element": True}),
            ("click", {"tab": 1, "element": 0}),
            ("click", {"tab": "1", "element": 2}),
            ("type", {"tab": 1, "element": 2, "text": ""}),
            ("type", {"tab": 1, "element": 2, "text": "x" * 2001}),
            ("type", {"tab": 1, "element": 2, "text": "x", "submit": "yes"}),
            ("scroll", {"tab": 1, "pages": 0}), ("scroll", {"tab": 1, "pages": 11}),
            ("read_page", {"tab": 1, "max_chars": 10})]:
        with pytest.raises(browser.BrowserError):
            v(verb, params)
    with pytest.raises(browser.BrowserError, match="Jav3 server"):
        v("open_tab", {"url": "http://jav3.lan:8000/settings"},
          browser._own_hosts("jav3.lan:8000"))


async def test_secrets_never_go_to_the_browser(env, monkeypatch):
    from backend import secrets as secrets_mod
    monkeypatch.setattr(secrets_mod, "find_in_bytes",
                        lambda b: ["API_KEY"] if b"sk-live-123" in b else [])
    for verb, params in [("type", {"tab": 1, "element": 2, "text": "sk-live-123"}),
                         ("open_tab", {"url": "https://x.example/?k=sk-live-123"})]:
        with pytest.raises(browser.BrowserError, match="API_KEY"):
            browser.validate(verb, params)


# --- scope ----------------------------------------------------------------------------

async def test_scope_keeps_browser_apart(env):
    # token in the first frame; garbage / missing token closed 4401
    ws = WS()
    assert (await ws.handshake())["type"] == "websocket.accept"
    await ws.send({"type": "hello", "token": "jvd_" + "x" * 40})
    assert (await ws.recv())["code"] == 4401
    # desk and cli tokens are refused at the browser door, with an event
    for tok in (env["dtok"], env["ctok"]):
        ws = WS()
        await ws.handshake()
        await ws.send({"type": "hello", "token": tok})
        assert (await ws.recv())["code"] == 4403
    assert await _events("browser_refused")
    async with httpx.AsyncClient(transport=env["transport"],
                                 base_url="http://jav3.lan:8000") as dev:
        h = {"Authorization": f"Bearer {env['btok']}"}
        # a browser token cannot start chat turns or list conversations
        assert (await dev.post("/api/chat", json={"message": "hi"}, headers=h)).status_code == 403
        assert (await dev.get("/api/conversations", headers=h)).status_code == 403
        # nor reach the operator routes, nor the desk socket
        assert (await dev.get("/api/browser", headers=h)).status_code == 401
        r = await dev.put(f"/api/browser/{env['bid']}/grants",
                          json={"project": "*", "read": True}, headers=h)
        assert r.status_code == 401
    ws = WS(path="/api/desk/ws", headers=[(b"authorization", f"Bearer {env['btok']}".encode())])
    assert (await ws.handshake())["code"] == 4403


async def test_redeem_for_browser_scope(env):
    code, _ = pastelogin.mint("chrome", by="operator")
    async with httpx.AsyncClient(transport=env["transport"],
                                 base_url="http://jav3.lan:8000") as dev:
        r = await dev.post("/api/devices/login", json={"code": code, "scope": "browser"})
        assert r.status_code == 200 and r.json()["scope"] == "browser"
    assert (await devicetokens.verify(r.json()["token"]))["scope"] == "browser"


async def test_ws_with_operator_cookie_from_extension_origin(env):
    """The extension's socket carries the Jav3 cookie from chrome-extension://;
    the route is origin-exempt because it never reads the cookie."""
    cookie = "; ".join(f"{k}={v}" for k, v in env["op"].cookies.items())
    fe = FakeExt(env["btok"], headers=[(b"origin", b"chrome-extension://abcdef"),
                                       (b"cookie", cookie.encode())])
    await fe.start()
    assert "jav3.lan" in fe.welcome["deny_hosts"]
    await fe.stop()


# --- grants, routing, taint -------------------------------------------------------------

async def test_per_project_grants_and_routing(env, monkeypatch):
    monkeypatch.setattr(browser, "ACTIONS_PER_S", 100)
    fe = await FakeExt(env["btok"]).start()
    try:
        r = await _tool("browser_open_tab")(url="https://example.com/")
        assert r.startswith("error:") and "project 'proj'" in r and not fe.reqs
        await _grant(env, project="other")
        assert (await _tool("browser_open_tab")(url="https://example.com/")).startswith("error:")
        await _grant(env, project="*")                    # every project
        r = await _tool("browser_open_tab")(url="https://example.com/")
        assert "tab 7" in r and fe.reqs[-1]["verb"] == "open_tab"
        assert fe.reqs[-1]["params"] == {"url": "https://example.com/"}
        # read granted, act not
        assert "Act" in await _tool("browser_click")(tab=7, element=1)
        await _grant(env, project="proj", read=False, act=True)   # act implies read
        g = (await env["op"].get("/api/browser")).json()["browsers"][0]
        assert g["online"] and {"project": "proj", "read": True, "act": True} in [
            {k: x[k] for k in ("project", "read", "act")} for x in g["grants"]]
        # no blind input: click needs a read of that tab in this turn
        tok = budget_mod.active_op_id.set("op-b1")
        try:
            r = await _tool("browser_click")(tab=7, element=1)
            assert "read the tab first" in r
            page = await _tool("browser_read_page")(tab=7)
            assert "UNTRUSTED" in page and "[2] input 'q'" in page
            assert "tab 7" in await _tool("browser_click")(tab=7, element=1)
            assert "tab 7" in await _tool("browser_type")(tab=7, element=2, text="hi", submit=True)
            assert fe.reqs[-1]["params"] == {"tab": 7, "element": 2, "text": "hi", "submit": True}
            shot = await _tool("browser_screenshot_tab")(tab=7)
            assert "screenshot 800x600" in shot
            assert "tab 7" in await _tool("browser_list_tabs")()
            await _tool("browser_navigate")(tab=7, url="https://example.org/")
            assert "read the tab first" in await _tool("browser_click")(tab=7, element=1)
        finally:
            budget_mod.active_op_id.reset(tok)
            broker._tainted.discard("op-b1")
        db = await get_db()
        try:
            async with db.execute("SELECT verb, params FROM browser_actions "
                                  "WHERE verb='type'") as cur:
                row = await cur.fetchone()
        finally:
            await db.close()
        assert "hi" not in json.loads(row["params"]).values()     # length + digest only
    finally:
        await fe.stop()


async def test_every_browser_result_taints_the_turn(env):
    fe = await FakeExt(env["btok"]).start()
    try:
        await _grant(env)
        tok = budget_mod.active_op_id.set("op-bt")
        try:
            assert not broker.op_tainted("op-bt")
            await _tool("browser_list_tabs")()
            assert broker.op_tainted("op-bt")
        finally:
            budget_mod.active_op_id.reset(tok)
            broker._tainted.discard("op-bt")
        assert all(broker.classify_taint("browser_" + v) == "untrusted"
                   for v in browser.VERBS)
    finally:
        await fe.stop()


async def test_tools_offered_only_with_a_browser_connected(env):
    names = lambda: {s["function"]["name"] for s in registry.openai_tool_specs()}  # noqa: E731
    assert not any(n.startswith("browser_") for n in names())
    fe = await FakeExt(env["btok"]).start()
    try:
        assert {"browser_" + v for v in browser.VERBS} <= names()
    finally:
        await fe.stop()


async def test_pause_cancel_and_stop(env):
    fe = await FakeExt(env["btok"]).start()
    try:
        await _grant(env)
        await fe.ws.send({"type": "state", "paused": True})
        for _ in range(50):
            if browser.connected()[0].paused:
                break
            await asyncio.sleep(0.02)
        assert "paused" in await _tool("browser_list_tabs")() and not fe.reqs
        assert await _events("browser_paused")
        await fe.ws.send({"type": "state", "paused": False})
        await asyncio.sleep(0.05)

        async def cancelled(m):
            return {"ok": False, "code": "cancelled", "err": "cancelled by the operator"}
        fe.answer = cancelled
        assert "cancelled" in await _tool("browser_list_tabs")()
        assert browser.connected()[0].paused
        # Stop from Settings: grants gone, socket killed
        r = await env["op"].post(f"/api/browser/{env['bid']}/stop")
        assert r.status_code == 200 and not browser.connected()
        assert (await env["op"].get("/api/browser")).json()["browsers"][0]["grants"] == []
    finally:
        await fe.stop()


