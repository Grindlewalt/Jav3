"""The model-call ledger + cost accounting: every API call is recorded at the
Model.complete choke point (usage always; raw context only when the operator
flips capture on), and the Logs cost endpoints price it with the configured
per-million rates."""
import json

import httpx
import pytest

from backend import runtime
from backend.agent.model import record_model_call
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db, set_state
from backend.main import app
from backend.memory import ensure_memory_seeds

USAGE = {"prompt_tokens": 1000, "completion_tokens": 200,
         "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100}
MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


async def _rows():
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM model_calls ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def test_usage_always_recorded_context_only_when_captured(tmp_env):
    await init_db()
    await record_model_call(7, "deepseek-v4-flash", USAGE, MSGS, tools=[{}, {}])
    rows = await _rows()
    assert rows[0]["conversation_id"] == 7
    assert rows[0]["input_tokens"] == 1000 and rows[0]["cache_hit"] == 900
    assert rows[0]["context"] is None          # capture defaults OFF

    db = await get_db()
    try:
        await set_state(db, "capture_context", "1")
        await db.commit()
    finally:
        await db.close()
    await record_model_call(7, "deepseek-v4-flash", USAGE, MSGS, tools=[{}, {}])
    rows = await _rows()
    ctx = json.loads(rows[1]["context"])
    assert ctx["messages"] == MSGS and ctx["n_tools"] == 2


async def test_incognito_records_spend_but_never_content(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await set_state(db, "capture_context", "1")
        await db.commit()
    finally:
        await db.close()
    tok = runtime.ephemeral.set(True)
    try:
        await record_model_call(7, "m", USAGE, MSGS, tools=None)
    finally:
        runtime.ephemeral.reset(tok)
    rows = await _rows()
    assert rows[0]["conversation_id"] is None    # unattributed
    assert rows[0]["context"] is None            # no trace, even with capture on
    assert rows[0]["input_tokens"] == 1000       # but the spend is counted


async def test_context_blobs_age_out(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO model_calls (conversation_id, context, created_at) "
            "VALUES (1, '{\"messages\": []}', datetime('now', '-30 days'))")
        await db.commit()
    finally:
        await db.close()
    await record_model_call(1, "m", USAGE, MSGS, tools=None)  # prunes on insert
    rows = await _rows()
    assert rows[0]["context"] is None            # aged out
    assert rows[0]["input_tokens"] == 0          # usage row itself kept


async def test_cost_endpoints_price_the_ledger(client):
    for _ in range(3):
        await record_model_call(5, "m", USAGE, MSGS, tools=None)

    r = (await client.get("/api/logs/costs")).json()
    w = r["windows"]["24h"]
    assert w["calls"] == 3
    assert w["cache_hit"] == 2700 and w["cache_miss"] == 300 and w["output"] == 600
    expected = (2700 * settings.price_cache_hit_per_m
                + 300 * settings.price_cache_miss_per_m
                + 600 * settings.price_output_per_m) / 1_000_000
    assert w["cost_usd"] == round(expected, 4)
    assert r["windows"]["all"]["calls"] == 3
    assert r["capture_context"] is False
    assert r["prices_per_m"]["output"] == settings.price_output_per_m

    # per-conversation drill-down
    calls = (await client.get("/api/logs/conversations/5/calls")).json()["calls"]
    assert len(calls) == 3 and calls[0]["has_context"] is False
    assert calls[0]["cost_usd"] > 0

    # context 404s until captured
    r404 = await client.get(f"/api/logs/calls/{calls[0]['id']}/context")
    assert r404.status_code == 404

    # flip capture on via the API, record another call, read its context back
    ok = await client.post("/api/logs/capture-context", json={"enabled": True})
    assert ok.json()["enabled"] is True
    await record_model_call(5, "m", USAGE, MSGS, tools=[{}])
    calls = (await client.get("/api/logs/conversations/5/calls")).json()["calls"]
    assert calls[-1]["has_context"] is True
    ctx = (await client.get(f"/api/logs/calls/{calls[-1]['id']}/context")).json()
    assert ctx["messages"] == MSGS and ctx["n_tools"] == 1
    assert ctx["input_tokens"] == 1000
