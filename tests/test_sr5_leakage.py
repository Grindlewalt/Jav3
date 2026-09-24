"""SR5 (leakage / DoS) PoCs against the paste-code login. Findings, not fixes:
tests named *_poc assert the CURRENT (vulnerable) behaviour so a fixer sees them
flip; tests named *_holds are negative results that should stay green."""
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
    """Attacker behind the same NAT / reverse proxy / ssh tunnel as the operator:
    10 junk codes and the operator's VALID code is refused 429 for 10 min —
    as long as the code's TTL, so the code dies unused. Renewable forever."""
    async with _dev("10.0.0.1") as shared:
        for _ in range(pastelogin._WRONG_PER_PEER):
            assert (await shared.post(LOGIN, json={"code": BAD})).status_code == 401
        code = (await op.post(MINT, json={})).json()["code"]
        r = await shared.post(LOGIN, json={"code": code})
        assert r.status_code == 429


async def test_global_miss_budget_lockout_poc(op):
    """50 source addresses x 10 misses trips _WRONG_GLOBAL; then a valid code
    from a peer that never missed is refused. One LAN host can hold 50 IPv4
    aliases, so this is one machine's work (500 requests)."""
    for i in range(pastelogin._WRONG_GLOBAL // pastelogin._WRONG_PER_PEER):
        async with _dev(f"10.1.{i // 250}.{i % 250 + 1}") as d:
            for _ in range(pastelogin._WRONG_PER_PEER):
                await d.post(LOGIN, json={"code": BAD})
    code = (await op.post(MINT, json={})).json()["code"]
    async with _dev("192.168.1.77") as fresh:
        assert (await fresh.post(LOGIN, json={"code": code})).status_code == 429


async def test_oversized_body_parsed_before_throttle_poc(op):
    """Body validation runs before the throttle: a locked-out peer still gets
    its whole body buffered and parsed (422, not 429), and the 422 reflects the
    oversized input back — unauthenticated, unthrottled work and bandwidth."""
    async with _dev("10.0.0.2") as d:
        for _ in range(pastelogin._WRONG_PER_PEER):
            await d.post(LOGIN, json={"code": BAD})
        assert (await d.post(LOGIN, json={"code": BAD})).status_code == 429
        big = "x" * 2_000_000
        r = await d.post(LOGIN, json={"code": big})
        assert r.status_code == 422
        assert len(r.content) > 2_000_000          # echoed back


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
