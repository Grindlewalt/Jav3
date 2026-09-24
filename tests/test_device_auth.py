"""Paste-code login: an authenticated session mints a single-use code, a CLI
trades it for a revocable device token (sha256-stored), and that token reaches
the require_actor routers (chat) but never the cookie-only control plane."""
import asyncio

import httpx
import pytest

from backend import devices_api, devicetokens, pastelogin
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app

LOGIN = "/api/devices/login"
MINT = "/api/devices/login-code"


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
    # op = logged-in operator (cookie jar); dev = a computer with no session
    async with httpx.AsyncClient(transport=transport, base_url="http://jav3.lan:8000") as op, \
               httpx.AsyncClient(transport=transport, base_url="http://jav3.lan:8000") as dev:
        await op.post("/api/auth/login",
                      json={"username": "operator", "password": "hunter2"})
        yield op, dev


async def _mint(op) -> dict:
    r = await op.post(MINT, json={})
    assert r.status_code == 200, r.text
    return r.json()


async def _redeem(dev, code, **kw):
    return await dev.post(LOGIN, json={"code": code, "hostname": "box",
                                       "platform": "linux", **kw})


# --- token store (unit) -------------------------------------------------------

async def test_token_mint_verify_revoke(tmp_env):
    await init_db()
    raw, tid = await devicetokens.mint("cli", hostname="box", platform="linux")
    assert raw.startswith("jvd_")
    who = await devicetokens.verify(raw)
    assert who and who["device_id"] == tid and who["name"] == "cli"
    assert await devicetokens.verify("jvd_" + "x" * 40) is None   # wrong token
    assert await devicetokens.verify("not-a-token") is None       # bad format
    assert await devicetokens.revoke(tid) is True
    assert await devicetokens.verify(raw) is None                 # revoked → dead
    assert await devicetokens.revoke(tid) is False                # idempotent


# --- ticket store (unit) -------------------------------------------------------

def test_code_entropy_and_hashed_storage():
    code, t = pastelogin.mint("x", by="operator")
    assert len(code) >= 43                       # token_urlsafe(32): 256 bits
    # the raw code is not kept anywhere in the store — only its digest
    assert code not in repr(pastelogin._tickets)
    assert t.digest in pastelogin._tickets and t.kind == pastelogin.KIND


def test_redeem_single_use_and_expiry():
    code, _ = pastelogin.mint(by="op", now=1000.0)
    assert pastelogin.redeem(code, now=1001.0) is not None
    assert pastelogin.redeem(code, now=1002.0) is None           # spent
    code2, _ = pastelogin.mint(by="op", now=1000.0)
    assert pastelogin.redeem(code2, now=1000.0 + pastelogin.TTL_SECONDS) is None


def test_redeem_rejects_malformed_without_lookup():
    for bad in (None, "", "short", "x" * 500, "a b" * 20, 12345, "é" * 40):
        assert pastelogin.redeem(bad) is None


def test_live_tickets_are_bounded():
    for _ in range(pastelogin.MAX_LIVE + 10):
        pastelogin.mint(by="op")
    assert pastelogin.live_count() == pastelogin.MAX_LIVE


# --- full HTTP flow -----------------------------------------------------------

async def test_mint_requires_cookie_session(clients):
    op, dev = clients
    assert (await dev.post(MINT, json={})).status_code == 401
    # a device token is not a session: it cannot mint more codes
    tok = (await _redeem(dev, (await _mint(op))["code"])).json()["token"]
    r = await dev.post(MINT, json={}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


async def test_mint_is_csrf_guarded(clients):
    op, _ = clients
    bad = await op.post(MINT, json={}, headers={"Origin": "http://evil.example"})
    assert bad.status_code == 403
    ok = await op.post(MINT, json={}, headers={"Origin": "http://jav3.lan:8000"})
    assert ok.status_code == 200


async def test_login_line_shape_and_address(clients):
    op, _ = clients
    m = await _mint(op)
    assert m["login"] == f"address=jav3.lan:8000 code={m['code']}"
    assert m["ttl_seconds"] == pastelogin.TTL_SECONDS
    r = await op.post(MINT, json={})
    assert r.headers.get("cache-control") == "no-store"


async def test_loopback_host_is_replaced_by_a_lan_address(clients, monkeypatch):
    op, _ = clients
    monkeypatch.setattr(devices_api.lan, "lan_ips", lambda: ["192.168.1.20"])
    monkeypatch.setattr(devices_api.lan, "advertised_hostname", lambda: "")
    r = await op.post(MINT, json={}, headers={"Host": "127.0.0.1:8000"})
    assert r.json()["address"] == "192.168.1.20:8000"
    monkeypatch.setattr(devices_api.lan, "advertised_hostname", lambda: "jav3.local")
    r = await op.post(MINT, json={}, headers={"Host": "localhost:8000"})
    assert r.json()["address"] == "jav3.local:8000"


async def test_hostile_host_header_is_not_echoed(clients, monkeypatch):
    op, _ = clients
    monkeypatch.setattr(devices_api.lan, "lan_ips", lambda: ["10.0.0.9"])
    monkeypatch.setattr(devices_api.lan, "advertised_hostname", lambda: "")
    r = await op.post(MINT, json={}, headers={"Host": "x code=evil"})
    assert r.json()["address"] == "10.0.0.9:8000"


async def test_code_is_single_use_and_token_works_on_chat_only(clients):
    op, dev = clients
    code = (await _mint(op))["code"]
    r = await _redeem(dev, code)
    assert r.status_code == 200 and r.headers.get("cache-control") == "no-store"
    token = r.json()["token"]
    assert token.startswith("jvd_")
    # replay: same generic 401 as a code that never existed
    again = await _redeem(dev, code)
    never = await _redeem(dev, "A" * 43)
    assert again.status_code == never.status_code == 401
    assert again.json() == never.json()

    hdr = {"Authorization": f"Bearer {token}"}
    who = await dev.get("/api/devices/whoami", headers=hdr)
    assert who.status_code == 200 and who.json()["is_device"] is True
    assert (await dev.get("/api/conversations", headers=hdr)).status_code == 200
    # cookie-only control plane refuses it
    for path in ("/api/secrets", "/api/devices", "/api/model"):
        assert (await dev.get(path, headers=hdr)).status_code == 401, path


async def test_concurrent_redeems_mint_exactly_one_token(clients):
    op, dev = clients
    code = (await _mint(op))["code"]
    rs = await asyncio.gather(*(_redeem(dev, code) for _ in range(8)))
    assert sorted(r.status_code for r in rs) == [200] + [401] * 7
    assert len((await op.get("/api/devices")).json()["devices"]) == 1


async def test_expired_code_fails(clients, monkeypatch):
    op, dev = clients
    code = (await _mint(op))["code"]
    real = pastelogin.time.time
    monkeypatch.setattr(pastelogin.time, "time",
                        lambda: real() + pastelogin.TTL_SECONDS + 1)
    assert (await _redeem(dev, code)).status_code == 401


async def test_wrong_codes_are_throttled_per_peer(clients):
    op, dev = clients
    for _ in range(pastelogin._WRONG_PER_PEER):
        assert (await _redeem(dev, "B" * 43)).status_code == 401
    # budget spent: refused before the code is even looked at — a valid one too
    good = (await _mint(op))["code"]
    assert (await _redeem(dev, good)).status_code == 429
    # forged forwarding headers do not buy a fresh budget
    r = await dev.post(LOGIN, json={"code": good},
                       headers={"X-Forwarded-For": "9.9.9.9",
                                "CF-Connecting-IP": "8.8.8.8"})
    assert r.status_code == 429


def test_global_call_budget():
    for i in range(pastelogin._CALLS_GLOBAL):
        pastelogin.throttle(f"peer-{i % 50}", now=5000.0)
    with pytest.raises(pastelogin.TooMany):
        pastelogin.throttle("fresh-peer", now=5000.0)


async def test_revoke_kills_token_and_self_revoke(clients):
    op, dev = clients
    t1 = (await _redeem(dev, (await _mint(op))["code"])).json()
    t2 = (await _redeem(dev, (await _mint(op))["code"])).json()
    h1 = {"Authorization": f"Bearer {t1['token']}"}
    h2 = {"Authorization": f"Bearer {t2['token']}"}
    # operator revokes #1 from Settings
    assert (await op.delete(f"/api/devices/{t1['device_id']}")).status_code == 200
    assert (await dev.get("/api/devices/whoami", headers=h1)).status_code == 401
    # #2 revokes itself (jav3 logout) — and only itself
    assert (await dev.delete("/api/devices/self", headers=h2)).status_code == 200
    assert (await dev.get("/api/devices/whoami", headers=h2)).status_code == 401
    # a device cannot use the operator's revoke-by-id route
    t3 = (await _redeem(dev, (await _mint(op))["code"])).json()
    h3 = {"Authorization": f"Bearer {t3['token']}"}
    assert (await dev.delete(f"/api/devices/{t3['device_id']}",
                             headers=h3)).status_code == 401
    # the cookie session cannot "revoke self"
    assert (await op.delete("/api/devices/self")).status_code == 400


async def test_cli_files_are_served(clients):
    _, dev = clients
    r = await dev.get("/cli/install.sh")
    assert r.status_code == 200
    assert 'BASE="http://jav3.lan:8000"' in r.text and "@@BASE@@" not in r.text
    r = await dev.get("/cli/jav3")
    assert r.status_code == 200 and "def main(" in r.text
    assert (await dev.get("/cli/install.sh",
                          headers={"Host": "a;rm -rf ~"})).status_code == 400
