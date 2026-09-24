"""SR1 adversarial review — angle: AUTH BYPASS / privilege reach.

An authorized red-team of the paste-code device login (commits 953bc54..53133b9)
and the surrounding auth surface. Each test is named for what it proves:

  test_BYPASS_*   a PASSING test that DEMONSTRATES a real bypass (a finding).
  test_BLOCKED_*  a PASSING test that shows an attack is refused (regression
                  coverage for the defences that already hold).
  test_REACH_*    proves the blast radius of a credential that is legitimately
                  issued but broader / longer-lived than its docs imply.

Run:
  .venv/bin/python -m pytest -q tests/test_sr1_authbypass.py
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from backend import auth, devices_api, devicetokens, pastelogin
from backend.auth import COOKIE_NAME, hash_password
from backend.config import get_jwt_secret, settings
from backend.db import get_db, init_db
from backend.main import app

MINT = "/api/devices/login-code"
LOGIN = "/api/devices/login"

# Every control-plane router that is documented as cookie-only (require_user)
# and must therefore reject a device bearer token. Path + a method that exists.
CONTROL_PLANE = [
    ("GET", "/api/secrets"),
    ("GET", "/api/vm/status"),
    ("GET", "/api/egress/pending"),
    ("GET", "/api/backup/config"),
    ("GET", "/api/memory"),
    ("GET", "/api/projects"),
    ("GET", "/api/schedules"),
    ("GET", "/api/artifacts"),
    ("GET", "/api/reviewer"),
    ("GET", "/api/lan"),
    ("GET", "/api/model"),
    ("GET", "/api/security/events"),
]


@pytest.fixture(autouse=True)
def _reset():
    pastelogin.reset_for_tests()
    yield
    pastelogin.reset_for_tests()


@pytest.fixture
async def clients(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://jav3.lan:8000") as op, \
               httpx.AsyncClient(transport=transport, base_url="http://jav3.lan:8000") as dev:
        await op.post("/api/auth/login",
                      json={"username": "operator", "password": "hunter2"})
        yield op, dev


async def _device_token(op, dev) -> str:
    code = (await op.post(MINT, json={})).json()["code"]
    r = await dev.post(LOGIN, json={"code": code, "hostname": "box", "platform": "linux"})
    return r.json()["token"]


# ---------------------------------------------------------------------------
# CRITICAL: the SPA static catch-all reads arbitrary files, leaking the JWT
# signing secret, which forges an operator cookie -> the whole control plane.
# ---------------------------------------------------------------------------

def test_BYPASS_spa_static_route_reads_files_outside_dist(tmp_path, monkeypatch):
    """FIXED (on main, backend.main.dist_file): the SPA route resolves the path
    and refuses anything outside frontend/dist — `..` traversal and absolute
    paths both fall back to the shell. Was: unauthenticated arbitrary file
    read (`GET /../data/jwt_secret`, `GET //etc/hostname`)."""
    from backend import main
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>")
    secret_file = tmp_path / "data" / "jwt_secret"
    secret_file.parent.mkdir()
    secret_file.write_text("TOP-SECRET-SIGNING-KEY")
    monkeypatch.setattr(settings, "frontend_dist", dist)

    assert main.dist_file("../data/jwt_secret") is None
    assert main.dist_file("/etc/hostname") is None
    assert main.dist_file("assets/../../data/jwt_secret") is None
    (dist / "assets" / "app.js").write_text("x")
    assert main.dist_file("assets/app.js") == dist / "assets" / "app.js"


async def test_BYPASS_leaked_jwt_secret_forges_operator_session(clients):
    """FIXED at the leak (dist_file above). What remains true by construction:
    a cookie is only as good as the signing key, so a cookie signed with any
    OTHER key is refused on every control-plane route, and the real key is
    not reachable through the SPA route. (Rotating a key that may already
    have leaked from a deployed box is the operator's call.)"""
    import jwt as pyjwt
    from datetime import datetime, timedelta, timezone
    op, dev = clients
    forged = pyjwt.encode({"sub": "1", "username": "operator",
                           "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                          "not-the-server-key-" + "x" * 32, algorithm="HS256")
    dev.cookies.set(COOKIE_NAME, forged, domain="jav3.lan")
    for method, path in CONTROL_PLANE:
        r = await dev.request(method, path)
        assert r.status_code == 401, (path, r.status_code)
    get_jwt_secret()                                   # materialise the key file
    r = await dev.get("/../data/jwt_secret")
    assert "TOP-SECRET" not in r.text and get_jwt_secret() not in r.text


# ---------------------------------------------------------------------------
# REACH: the device token is broader / more persistent than devicetokens.py's
# docstring ("individually revocable", operator can "spot a stale one") implies.
# ---------------------------------------------------------------------------

async def test_REACH_device_token_never_expires(tmp_env):
    """FINDING (medium): a device token has no expiry. `device_tokens` has no
    expiry column and `verify()` only checks `revoked = 0`, so a token minted
    once is valid forever unless the operator manually revokes it. The operator
    login cookie expires (jwt_ttl_hours) but a stolen device token does not.
    """
    await init_db()
    raw, tid = await devicetokens.mint("cli")
    # simulate the row being a year old — still verifies
    db = await get_db()
    try:
        await db.execute(
            "UPDATE device_tokens SET created_at = datetime('now','-400 days'), "
            "last_seen = datetime('now','-400 days') WHERE id = ?", (tid,))
        await db.commit()
    finally:
        await db.close()
    assert await devicetokens.verify(raw) is not None      # no TTL anywhere


async def test_REACH_device_token_survives_deletion_of_minting_user(clients):
    """FINDING (medium): a device token carries no user id (id=-1) and no FK to
    the user who minted it (`paired_by` is a free-text username string). Deleting
    that operator account does NOT invalidate the token — it keeps
    operator-equivalent chat/tool reach with no live account behind it.
    """
    op, dev = clients
    tok = await _device_token(op, dev)
    db = await get_db()
    try:
        await db.execute("DELETE FROM users WHERE username = 'operator'")
        await db.commit()
    finally:
        await db.close()
    # the account is gone, yet the token still authenticates as an actor
    r = await dev.get("/api/devices/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json().get("is_device") is True


async def test_REACH_device_token_reads_and_deletes_all_conversations(clients):
    """FINDING (medium/info, by design but under-appreciated): a device bearer on
    the require_actor router can enumerate, read the full transcript of, and
    DELETE any conversation — not just its own. `require_actor` returns a fixed
    id=-1 device actor and none of the /api/conversations handlers scope by
    actor. A stolen CLI token is thus a read/destroy primitive over all chats.
    """
    op, dev = clients
    tok = await _device_token(op, dev)
    h = {"Authorization": f"Bearer {tok}"}
    # operator creates a conversation row directly
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO conversations (kind, summary) VALUES ('chat','op secret chat')")
        cid = cur.lastrowid
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
            (cid, "operator private message"))
        await db.commit()
    finally:
        await db.close()
    listed = await dev.get("/api/conversations", headers=h)
    assert any(c["id"] == cid for c in listed.json()["conversations"])
    msgs = await dev.get(f"/api/conversations/{cid}/messages", headers=h)
    assert "operator private message" in msgs.text
    gone = await dev.delete(f"/api/conversations/{cid}", headers=h)
    assert gone.status_code == 200


# ---------------------------------------------------------------------------
# BLOCKED: attacks the design already refuses. These lock in the good behaviour.
# ---------------------------------------------------------------------------

async def test_BLOCKED_device_bearer_cannot_reach_control_plane(clients):
    """A device token is refused (401) on every cookie-only control-plane
    router — the core promise of require_actor vs require_user."""
    op, dev = clients
    tok = await _device_token(op, dev)
    h = {"Authorization": f"Bearer {tok}"}
    for method, path in CONTROL_PLANE:
        r = await dev.request(method, path, headers=h)
        assert r.status_code == 401, f"{method} {path} let a device token in ({r.status_code})"


async def test_BLOCKED_bearer_cannot_mint_new_login_code(clients):
    """A device token is not a session: it cannot mint fresh login codes (which
    would let one stolen token bootstrap unlimited new devices)."""
    op, dev = clients
    tok = await _device_token(op, dev)
    r = await dev.post(MINT, json={}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


async def test_BLOCKED_mint_requires_same_origin(clients):
    """A cross-origin page cannot ride the operator's cookie to mint a code."""
    op, _ = clients
    bad = await op.post(MINT, json={}, headers={"Origin": "http://evil.example"})
    assert bad.status_code == 403


async def test_BLOCKED_code_is_single_use(clients):
    """A code redeems exactly once; the second redeem is the generic 401."""
    op, dev = clients
    code = (await op.post(MINT, json={})).json()["code"]
    first = await dev.post(LOGIN, json={"code": code})
    assert first.status_code == 200
    second = await dev.post(LOGIN, json={"code": code})
    assert second.status_code == 401


async def test_BLOCKED_revoked_token_rejected_by_require_actor(clients):
    """Once revoked, a token no longer authenticates as an actor."""
    op, dev = clients
    code = (await op.post(MINT, json={})).json()["code"]
    body = (await dev.post(LOGIN, json={"code": code})).json()
    tok, tid = body["token"], body["device_id"]
    assert await devicetokens.revoke(tid) is True
    r = await dev.get("/api/devices/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


async def test_BLOCKED_self_revoke_needs_a_device_not_a_cookie(clients):
    """DELETE /api/devices/self refuses a cookie session (no id confusion: it
    only ever revokes the presenting device token, never an operator)."""
    op, _ = clients
    r = await op.delete("/api/devices/self")     # operator cookie, no bearer
    assert r.status_code == 400


async def test_BLOCKED_device_cannot_revoke_arbitrary_token_id(clients):
    """The id-targeted revoke (DELETE /api/devices/{id}) is require_user, so a
    device token cannot revoke another device — no /self vs /{id} confusion."""
    op, dev = clients
    tok = await _device_token(op, dev)
    # a device bearer aimed at the numeric-id route -> 401 (cookie required)
    r = await dev.delete("/api/devices/1", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401
    # and the /self path is not reachable as an int id (route separation)
    r2 = await dev.delete("/api/devices/self", headers={"Authorization": f"Bearer {tok}"})
    assert r2.status_code == 200          # this is the device revoking ITSELF


async def test_BLOCKED_install_sh_refuses_hostile_host_header(clients):
    """The installer bakes the Host into a shell script; a Host with shell
    metacharacters is refused, not echoed."""
    op, _ = clients
    for bad in ["evil$(id)", "a b", 'x";curl evil|sh;"', "h'ost"]:
        r = await op.get("/cli/install.sh", headers={"Host": bad})
        assert r.status_code == 400, f"hostile Host {bad!r} accepted"
    good = await op.get("/cli/install.sh", headers={"Host": "jav3.lan:8000"})
    assert good.status_code == 200 and "jav3.lan:8000" in good.text


def test_BLOCKED_cli_never_follows_redirects():
    """The CLI client disables redirect-following, so a 3xx from a hostile or
    downgraded endpoint can never replay the bearer token elsewhere."""
    import importlib.util
    from importlib.machinery import SourceFileLoader
    src = Path(__file__).resolve().parent.parent / "clients" / "jav3cli" / "jav3"
    loader = SourceFileLoader("jav3cli", str(src))
    spec = importlib.util.spec_from_loader("jav3cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    c = mod._client("http://x", token="jvd_abc")
    assert c.follow_redirects is False
    # bare host:port is http (LAN default); an explicit https is preserved and
    # never silently downgraded
    assert mod.base_url("box:8000") == "http://box:8000"
    assert mod.base_url("https://box") == "https://box"
    with pytest.raises(mod.CliError):
        mod.base_url("ftp://box")
