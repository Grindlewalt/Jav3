"""Browser use (backend/browser.py + browser_api.py): a fake jav3-browser
extension over the REAL /api/browser/ws route, the operator's cookie routes and
the browser_* tool handlers — scope, per-project grants, the closed verb list,
read-before-act, pause/cancel, taint, routing and the extension zip."""
import asyncio
import base64
import io
import json
import zipfile

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
# every verb has a browser_<verb> tool except `forward` (browser_back forward=true)
TOOL_VERBS = set(browser.VERBS) - {"forward"}


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
                "frames": [{"index": 0, "host": "example.com", "url": "https://example.com/"},
                           {"index": 1, "host": "accounts.other.com",
                            "url": "https://accounts.other.com/"}],
                "elements": [
                    {"id": "f0:1", "tag": "a", "type": "", "role": "link", "name": "",
                     "text": "More", "box": {"x": 0, "y": 0, "w": 10, "h": 10}, "inView": True},
                    {"id": "f0:2", "tag": "input", "type": "text", "role": "", "name": "q",
                     "text": "", "box": {"x": 0, "y": 20, "w": 100, "h": 20}, "inView": True},
                    {"id": "f1:1", "tag": "button", "type": "", "role": "button",
                     "name": "Sign in", "text": "Sign in",
                     "box": {"x": 0, "y": 0, "w": 80, "h": 30}, "inView": False}]}}
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
    # a bare element number means the top frame; "fN:M" keeps its frame
    assert v("type", {"tab": 3, "element": 9, "text": "hi"}) == {
        "tab": 3, "element": "f0:9", "text": "hi", "submit": False}
    assert v("type", {"tab": 3, "element": "f2:5", "text": "hi"}) == {
        "tab": 3, "element": "f2:5", "text": "hi", "submit": False}
    assert v("scroll_to_element", {"tab": 1, "element": "f1:3"}) == {
        "tab": 1, "element": "f1:3"}
    assert v("scroll", {"tab": 1}) == {"tab": 1, "pages": 1}
    assert v("read_page", {"tab": 1}) == {"tab": 1, "max_chars": 8000, "wait_ms": 0}
    assert v("read_page", {"tab": 1, "wait_ms": 3000, "min_elements": 5, "selector": ".x"}) == {
        "tab": 1, "max_chars": 8000, "wait_ms": 3000, "min_elements": 5, "selector": ".x"}
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
            ("click", {"tab": 1, "element": "2"}),                  # not an fN:M id
            ("click", {"tab": 1, "element": "f1000:1"}),            # frame out of range
            ("scroll_to_element", {"tab": 1, "element": "x"}),
            ("click", {"tab": "1", "element": "f0:2"}),
            ("type", {"tab": 1, "element": 2, "text": ""}),
            ("type", {"tab": 1, "element": 2, "text": "x" * 2001}),
            ("type", {"tab": 1, "element": 2, "text": "x", "submit": "yes"}),
            ("scroll", {"tab": 1, "pages": 0}), ("scroll", {"tab": 1, "pages": 11}),
            ("read_page", {"tab": 1, "max_chars": 10}),
            ("read_page", {"tab": 1, "wait_ms": 20000}),
            ("read_page", {"tab": 1, "selector": ""}),
            ("read_page", {"tab": 1, "min_elements": 0})]:
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
            r = await _tool("browser_click")(tab=7, element="f0:1")
            assert "read the tab first" in r
            page = await _tool("browser_read_page")(tab=7)
            assert "UNTRUSTED" in page and '[f0:2] input:text "q" @ 0,20 100x20' in page
            assert "f1=accounts.other.com" in page and "[f1:1]" in page
            assert "off-screen" in page                        # the f1:1 button
            assert "tab 7" in await _tool("browser_click")(tab=7, element="f0:1")
            assert "tab 7" in await _tool("browser_type")(tab=7, element="f0:2", text="hi", submit=True)
            assert fe.reqs[-1]["params"] == {"tab": 7, "element": "f0:2", "text": "hi", "submit": True}
            shot = await _tool("browser_screenshot_tab")(tab=7)
            assert "screenshot 800x600" in shot
            assert "tab 7" in await _tool("browser_list_tabs")()
            await _tool("browser_navigate")(tab=7, url="https://example.org/")
            assert "read the tab first" in await _tool("browser_click")(tab=7, element="f0:1")
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
                   for v in TOOL_VERBS)
    finally:
        await fe.stop()


async def test_frame_scoped_click_needs_a_fresh_read(env, monkeypatch):
    """A click on an element in any frame ("f1:1") needs an all-frames read of
    that tab in this turn; one read covers every frame."""
    monkeypatch.setattr(browser, "ACTIONS_PER_S", 100)
    fe = await FakeExt(env["btok"]).start()
    try:
        await _grant(env, act=True)
        tok = budget_mod.active_op_id.set("op-fr")
        try:
            assert "read the tab first" in await _tool("browser_click")(tab=7, element="f1:1")
            page = await _tool("browser_read_page")(tab=7)
            assert "[f1:1]" in page                      # the iframe's button is listed
            assert "tab 7" in await _tool("browser_click")(tab=7, element="f1:1")
            assert fe.reqs[-1]["params"] == {"tab": 7, "element": "f1:1"}
            # scroll_to_element is element-bound too, so it also needs a read
            assert "tab 7" in await _tool("browser_scroll_to_element")(tab=7, element="f0:2")
        finally:
            budget_mod.active_op_id.reset(tok)
            broker._tainted.discard("op-fr")
    finally:
        await fe.stop()


async def test_popup_adoption_routing(env, monkeypatch):
    """When a Jav3 tab spawns a popup the extension adopts, the spawning action
    reports its tab id and list_tabs shows it."""
    monkeypatch.setattr(browser, "ACTIONS_PER_S", 100)
    fe = await FakeExt(env["btok"]).start()
    try:
        await _grant(env, act=True)

        async def with_popup(m):
            res = await FakeExt.default_answer(m)
            if m["verb"] == "click":
                res["data"]["opened"] = [{"tab": 12, "url": "https://accounts.other.com/o"}]
            if m["verb"] == "list_tabs":
                res["data"]["tabs"].append({"tab": 12, "url": "https://accounts.other.com/o",
                                            "title": "Sign in"})
            return res
        fe.answer = with_popup
        tok = budget_mod.active_op_id.set("op-pop")
        try:
            await _tool("browser_read_page")(tab=7)
            r = await _tool("browser_click")(tab=7, element="f0:1")
            assert "adopted popup tab" in r and "tab 12" in r
        finally:
            budget_mod.active_op_id.reset(tok)
            broker._tainted.discard("op-pop")
        assert "tab 12" in await _tool("browser_list_tabs")()
    finally:
        await fe.stop()


async def test_cross_origin_frame_consent_refusal_is_surfaced(env, monkeypatch):
    """The frame-consent rule lives in the extension (it holds the per-site
    decisions); a refusal to click into an un-allowed cross-origin frame is
    routed back to the model as an error, while a same-site click goes through."""
    monkeypatch.setattr(browser, "ACTIONS_PER_S", 100)
    fe = await FakeExt(env["btok"]).start()
    try:
        await _grant(env, act=True)

        async def refuse_frame(m):
            if m["verb"] == "click" and str(m["params"].get("element", "")).startswith("f1:"):
                return {"ok": False, "code": "failed",
                        "err": "the operator has not allowed accounts.other.com for this frame"}
            return await FakeExt.default_answer(m)
        fe.answer = refuse_frame
        tok = budget_mod.active_op_id.set("op-xo")
        try:
            await _tool("browser_read_page")(tab=7)
            r = await _tool("browser_click")(tab=7, element="f1:1")
            assert r.startswith("error:") and "accounts.other.com" in r
            assert "tab 7" in await _tool("browser_click")(tab=7, element="f0:1")
        finally:
            budget_mod.active_op_id.reset(tok)
            broker._tainted.discard("op-xo")
    finally:
        await fe.stop()


async def test_tools_offered_only_with_a_browser_connected(env):
    names = lambda: {s["function"]["name"] for s in registry.openai_tool_specs()}  # noqa: E731
    assert not any(n.startswith("browser_") for n in names())
    fe = await FakeExt(env["btok"]).start()
    try:
        assert {"browser_" + v for v in TOOL_VERBS} <= names()
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



async def test_extension_zip_is_served(env):
    r = await env["op"].get("/cli/jav3-browser.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "jav3-browser/manifest.json" in names and "jav3-browser/lib/verbs.js" in names
    assert "jav3-browser/lib/dom.js" in names         # injected into every frame
    assert not any("/test/" in n for n in names)


# --- navigation pass: select / hover / key / back, stale ids, changed, screenshot ids ---

def test_new_verbs_validate():
    v = browser.validate
    assert v("select", {"tab": 2, "element": "f0:7", "label": "UK", "x": 1}) == {
        "tab": 2, "element": "f0:7", "label": "UK"}
    assert v("select", {"tab": 2, "element": 7, "value": ""}) == {
        "tab": 2, "element": "f0:7", "value": ""}
    assert v("hover", {"tab": 2, "element": "f1:3"}) == {"tab": 2, "element": "f1:3"}
    assert v("key", {"tab": 2, "combo": "Shift+tab"}) == {"tab": 2, "combo": "shift+Tab"}
    assert v("key", {"tab": 2, "combo": "cmd+Ctrl+a"}) == {"tab": 2, "combo": "ctrl+super+a"}
    assert v("key", {"tab": 2, "combo": "Esc"}) == {"tab": 2, "combo": "Escape"}
    assert v("key", {"tab": 2, "combo": "ArrowDown"}) == {"tab": 2, "combo": "Down"}
    assert v("key", {"tab": 2, "combo": "f5"}) == {"tab": 2, "combo": "F5"}
    assert v("back", {"tab": 2, "url": "x"}) == {"tab": 2}
    assert v("forward", {"tab": 2}) == {"tab": 2}
    assert browser.VERBS["select"] == browser.VERBS["hover"] == browser.VERBS["key"] == "act"
    assert browser.VERBS["back"] == browser.VERBS["forward"] == "read"
    for verb, params in [
            ("select", {"tab": 1, "element": "f0:1"}),                         # neither
            ("select", {"tab": 1, "element": "f0:1", "value": "a", "label": "A"}),  # both
            ("select", {"tab": 1, "element": "f0:1", "label": " "}),
            ("select", {"tab": 1, "element": "f0:1", "value": 3}),
            ("select", {"tab": 1, "element": "f0:1", "label": "x" * 501}),
            ("select", {"tab": 1, "label": "UK"}),                             # no element
            ("hover", {"tab": 1, "element": "nope"}),
            ("key", {"tab": 1, "combo": ""}), ("key", {"tab": 1}),
            ("key", {"tab": 1, "combo": "hyper+a"}), ("key", {"tab": 1, "combo": "Enterr"}),
            ("key", {"tab": 1, "combo": "ctrl+alt+shift+super+altgr+a"}),      # 5 modifiers
            ("key", {"tab": 1, "combo": "ctrl+ a"}), ("key", {"tab": 1, "combo": "F25"}),
            ("back", {})]:
        with pytest.raises(browser.BrowserError):
            v(verb, params)


async def test_select_label_is_secret_checked(env, monkeypatch):
    from backend import secrets as secrets_mod
    monkeypatch.setattr(secrets_mod, "find_in_bytes",
                        lambda b: ["API_KEY"] if b"sk-live-123" in b else [])
    with pytest.raises(browser.BrowserError, match="API_KEY"):
        browser.validate("select", {"tab": 1, "element": 2, "value": "sk-live-123"})


def test_element_lines_select_options_icons_values():
    page = browser.render("read_page", {"tab": 3, "url": "https://ex.com/", "title": "T",
        "text": "hi", "elements": [
            {"id": "f0:9", "tag": "button", "name": "", "text": "", "icon": True,
             "box": {"x": 1, "y": 2, "w": 3, "h": 4}, "inView": False},
            {"id": "f0:7", "tag": "select", "name": "Country", "text": "",
             "options": [{"t": "US", "v": "us", "s": True}, {"t": "UK", "v": "uk", "s": False},
                         {"t": "DE", "v": "de", "s": False}], "more": 4,
             "box": {"x": 10, "y": 40, "w": 120, "h": 24}, "inView": True},
            {"id": "f0:8", "tag": "input", "type": "email", "name": "Email",
             "value": "a@b.c", "box": {"x": 0, "y": 0, "w": 9, "h": 9}, "inView": True},
            {"id": "f0:10", "tag": "input", "type": "checkbox", "name": "Remember me",
             "checked": True, "box": {"x": 0, "y": 0, "w": 9, "h": 9}, "inView": True}]},
        {"tab": 3}, changed=None, first=True)
    assert '[f0:7] select "Country" options: US*, UK, DE … (+4 more) @ 10,40 120x24' in page
    assert '[f0:8] input:email "Email" value="a@b.c"' in page
    assert '[f0:10] input:checkbox "Remember me" checked' in page
    assert "[f0:9] button (icon, no label) @ 1,2 3x4 off-screen" in page
    # in view first, whatever order the frames sent
    assert page.index("[f0:7]") < page.index("[f0:9]")
    assert page.rstrip().endswith("changed: unknown (first read of this tab)")


def test_screenshot_element_listing_scales_and_places_frames():
    b = browser.Browser(device_id=1, name="c", ws=None)
    browser._note_view(b, "read_page", 5, {
        "viewport": {"w": 400, "h": 300, "dpr": 2},
        "frames": [{"index": 0, "offset": {"x": 0, "y": 0}},
                   {"index": 1, "offset": {"x": 100, "y": 50}},
                   {"index": 2, "offset": None}],
        "elements": [
            {"id": "f0:12", "tag": "button", "name": "Sign in", "frame": 0,
             "box": {"x": 40, "y": 15, "w": 60, "h": 18}, "inView": True},
            {"id": "f0:13", "tag": "a", "name": "Far", "frame": 0,
             "box": {"x": 40, "y": 900, "w": 60, "h": 18}, "inView": False},
            {"id": "f1:1", "tag": "input", "type": "text", "name": "User", "frame": 1,
             "box": {"x": 10, "y": 10, "w": 50, "h": 20}, "inView": True},
            {"id": "f2:1", "tag": "button", "name": "Nested", "frame": 2,
             "box": {"x": 0, "y": 0, "w": 5, "h": 5}, "inView": True}]})
    out = browser.screenshot_elements(b.views[5], 800, 600)
    assert '[f0:12] button "Sign in" @ 80,30 120x36' in out
    assert '[f1:1] input "User" @ 220,120 100x40' in out
    assert "f0:13" not in out and "Nested" not in out and "1 element(s) in nested" in out
    browser._note_view(b, "click", 5, {})
    assert "shifted after the last click" in browser.screenshot_elements(b.views[5], 800, 600)
    browser._note_view(b, "scroll", 5, {})
    out = browser.screenshot_elements(b.views[5], 800, 600)
    assert "scrolled since the last browser_read_page" in out and "f0:12" not in out
    assert "no read of this tab" in browser.screenshot_elements(None, 800, 600)


async def test_stale_changed_key_and_screenshot_through_the_tools(env, monkeypatch):
    monkeypatch.setattr(browser, "ACTIONS_PER_S", 100)
    fe = await FakeExt(env["btok"]).start()
    state = {"sig": "0000abcd:3"}

    async def answer(m):
        verb, p = m["verb"], m["params"]
        if verb in ("click", "hover") and p.get("element") == "f0:2":
            return {"ok": False, "code": "stale", "err": "element is no longer on the page"}
        res = await FakeExt.default_answer(m)
        if verb == "read_page":
            res["data"]["viewport"] = {"w": 400, "h": 300, "dpr": 2}
            res["data"]["frames"][0]["offset"] = {"x": 0, "y": 0}
        if verb == "key":
            res["data"]["text"] = "pressed Enter on input \"q\"; submitted the form"
        if verb != "screenshot_tab":
            res["data"]["sig"] = state["sig"]
        return res
    fe.answer = answer
    try:
        await _grant(env, act=True)
        tok = budget_mod.active_op_id.set("op-nav")
        try:
            # key is input too: no blind keys
            assert "read the tab first" in await _tool("browser_key")(tab=7, combo="Enter")
            page = await _tool("browser_read_page")(tab=7, wait_ms=500)
            assert fe.reqs[-1]["params"]["wait_ms"] == 500
            assert page.rstrip().endswith("changed: unknown (first read of this tab)")
            r = await _tool("browser_click")(tab=7, element="f0:1")
            assert r.rstrip().endswith("changed: no")
            state["sig"] = "0000beef:4"
            r = await _tool("browser_key")(tab=7, combo="return")
            assert fe.reqs[-1]["params"] == {"tab": 7, "combo": "Return"}
            assert "submitted the form" in r and r.rstrip().endswith("changed: yes")
            r = await _tool("browser_click")(tab=7, element="f0:2")
            assert r == "error: element f0:2 is no longer on the page — browser_read_page again"
            assert "no longer on the page" in await _tool("browser_hover")(tab=7, element="f0:2")
            r = await _tool("browser_select")(tab=7, element="f0:1", label="UK")
            assert fe.reqs[-1]["params"] == {"tab": 7, "element": "f0:1", "label": "UK"}
            shot = await _tool("browser_screenshot_tab")(tab=7)
            assert '[f0:1] link "More" @ 0,0 20x20' in shot    # 800 px image / 400 px viewport
            assert "[f1:1]" not in shot                        # off-screen in the read
            await _tool("browser_back")(tab=7)
            assert fe.reqs[-1]["verb"] == "back"
            await _tool("browser_back")(tab=7, forward=True)
            assert fe.reqs[-1]["verb"] == "forward"
            # history moved: old ids are gone, and the screenshot says so
            assert "read the tab first" in await _tool("browser_click")(tab=7, element="f0:1")
            assert "went forward since" in await _tool("browser_screenshot_tab")(tab=7)
        finally:
            budget_mod.active_op_id.reset(tok)
            broker._tainted.discard("op-nav")
    finally:
        await fe.stop()
