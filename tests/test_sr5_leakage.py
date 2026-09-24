"""SR5 (leakage / DoS) review of the paste-code login. Tests named *_poc began
as PoCs of the vulnerable behaviour; the fixer (SF) inverted each to assert the
fix. Tests named *_holds are negative results that stay green."""
import logging

import httpx
import pytest

from backend import pastelogin
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app

LOGIN = "/api/devices/login"
MINT = "/api/devices/login-code"
BAD = "A" * 43


@pytest.fixture(autouse=True)
def _reset():
    pastelogin.reset_for_tests()
    yield
    pastelogin.reset_for_tests()


@pytest.fixture
async def op(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    t = httpx.ASGITransport(app=app, client=("192.168.1.10", 5000))
    async with httpx.AsyncClient(transport=t, base_url="http://jav3.lan:8000") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        yield c


def _dev(ip):
    t = httpx.ASGITransport(app=app, client=(ip, 40000))
    return httpx.AsyncClient(transport=t, base_url="http://jav3.lan:8000")


async def test_shared_peer_lockout_poc(op):
    """FIXED: an attacker behind the operator's NAT / proxy / ssh tunnel can
    spend the shared address's miss budget — further misses get 429 — but the
    operator's VALID code still redeems from that same address."""
    async with _dev("10.0.0.1") as shared:
        for _ in range(pastelogin._WRONG_PER_PEER):
            assert (await shared.post(LOGIN, json={"code": BAD})).status_code == 401
        assert (await shared.post(LOGIN, json={"code": BAD})).status_code == 429
        code = (await op.post(MINT, json={})).json()["code"]
        r = await shared.post(LOGIN, json={"code": code})
        assert r.status_code == 200


async def test_global_miss_budget_lockout_poc(op):
    """FIXED: 50 addresses x 10 misses trips _WRONG_GLOBAL for misses only; a
    valid code from a fresh peer still redeems."""
    for i in range(pastelogin._WRONG_GLOBAL // pastelogin._WRONG_PER_PEER):
        async with _dev(f"10.1.{i // 250}.{i % 250 + 1}") as d:
            for _ in range(pastelogin._WRONG_PER_PEER):
                await d.post(LOGIN, json={"code": BAD})
    code = (await op.post(MINT, json={})).json()["code"]
    async with _dev("192.168.1.77") as fresh:
        assert (await fresh.post(LOGIN, json={"code": BAD})).status_code == 429
        assert (await fresh.post(LOGIN, json={"code": code})).status_code == 200


async def test_oversized_body_parsed_before_throttle_poc(op):
    """FIXED: the redeem body is read by hand with a 4 KB cap — a declared
    Content-Length over it is 413 before any byte is read, a chunked body is
    cut off at the cap — and a malformed body gets one fixed 422 that echoes
    nothing back."""
    from backend import devices_api
    async with _dev("10.0.0.2") as d:
        big = "x" * 2_000_000
        r = await d.post(LOGIN, json={"code": big})
        assert r.status_code == 413 and len(r.content) < 200

        async def chunks():
            for _ in range(64):
                yield b"x" * 1024
        r = await d.post(LOGIN, content=chunks(),
                         headers={"Content-Type": "application/json"})
        assert r.status_code == 413
        marker = "ECHO-" + "Q" * 300
        for body in (b"{not json", f'{{"code": 5, "x": "{marker}"}}'.encode(),
                     f'{{"code": "{marker * 2}"}}'.encode()):
            r = await d.post(LOGIN, content=body,
                             headers={"Content-Type": "application/json"})
            assert r.status_code == 422, body[:30]
            assert "ECHO-" not in r.text and len(r.content) < 200
        assert len(body) < devices_api.MAX_LOGIN_BODY


def test_throttle_maps_bounded_holds():
    """Negative result: once the global miss budget trips, new peers are refused
    before any per-peer key is created, so the maps cannot grow without bound."""
    now = 1_000_000.0
    for i in range(pastelogin._WRONG_GLOBAL):
        pastelogin.note_wrong(f"p{i}", now)
    before = len(pastelogin._wrong) + len(pastelogin._calls)
    for i in range(5000):
        with pytest.raises(pastelogin.TooMany):
            pastelogin.throttle(f"new{i}", now)
    assert len(pastelogin._wrong) + len(pastelogin._calls) == before


def test_ticket_store_bounded_holds():
    for _ in range(1000):
        pastelogin.mint("x", by="operator")
    assert len(pastelogin._tickets) == pastelogin.MAX_LIVE


async def test_code_and_token_not_logged_holds(op, caplog):
    """Negative result: neither the code nor the minted token reaches any log
    record or the security_events feed."""
    caplog.set_level(logging.DEBUG)
    code = (await op.post(MINT, json={"name": "lap"})).json()["code"]
    async with _dev("192.168.1.20") as d:
        await d.post(LOGIN, json={"code": BAD})
        r = await d.post(LOGIN, json={"code": code, "hostname": "box"})
        assert r.status_code == 200
        token = r.json()["token"]
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert code not in text and token not in text
    db = await get_db()
    try:
        async with db.execute("SELECT summary, detail FROM security_events") as cur:
            rows = [f"{a} {b}" for a, b in await cur.fetchall()]
    finally:
        await db.close()
    assert rows and not any(code in r or token in r for r in rows)
