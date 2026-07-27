"""Runtime model switch: API allowlist + persistence, gateway resolution
(explicit pin > override > default), and per-model cost pricing. Offline."""
import httpx
import pytest

from backend.agent import model as model_mod
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


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
    model_mod.set_model_override(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/auth/login",
                         json={"username": "operator", "password": "hunter2"})
        assert r.status_code == 200
        yield c
    model_mod.set_model_override(None)


async def test_switch_api_and_persistence(client):
    r = await client.get("/api/model")
    assert r.json() == {"active": "deepseek-v4-flash",
                        "default": "deepseek-v4-flash",
                        "choices": ["deepseek-v4-flash", "deepseek-v4-pro"]}

    r = await client.put("/api/model", json={"model": "gpt-9"})
    assert r.status_code == 400

    r = await client.put("/api/model", json={"model": "deepseek-v4-pro"})
    assert r.status_code == 200
    assert r.json()["active"] == "deepseek-v4-pro"
    assert model_mod.get_model_override() == "deepseek-v4-pro"

    # persisted: a fresh load (as at app startup) restores it
    model_mod.set_model_override(None)
    await model_mod.load_model_override()
    assert model_mod.get_model_override() == "deepseek-v4-pro"

    # selecting the default clears the override entirely
    r = await client.put("/api/model", json={"model": "deepseek-v4-flash"})
    assert r.json()["active"] == "deepseek-v4-flash"
    assert model_mod.get_model_override() is None
    await model_mod.load_model_override()
    assert model_mod.get_model_override() is None


async def test_gateway_resolves_override(tmp_env, monkeypatch):
    seen = {}

    async def fake_complete(self, messages, tools=None, temperature=None,
                            model_name=None, base_url=None, key=None):
        seen["model"] = model_name
        yield {"type": "message", "content": "ok", "tool_calls": [],
               "usage": None}

    monkeypatch.setattr(model_mod.ModelClient, "complete", fake_complete)
    gw = model_mod.ModelGateway(api_key="test")

    async def run(**kw):
        async for _ in gw.complete([{"role": "user", "content": "hi"}], **kw):
            pass
        return seen["model"]

    model_mod.set_model_override(None)
    assert await run() == settings.model_name
    model_mod.set_model_override("deepseek-v4-pro")
    assert await run() == "deepseek-v4-pro"
    # an explicit per-call pin (agent model) beats the override
    assert await run(model_name="llama3:8b") == "llama3:8b"
    model_mod.set_model_override(None)


async def test_costs_priced_per_model(client):
    db = await get_db()
    try:
        for m, ch, cm, o in (("deepseek-v4-flash", 0, 1_000_000, 1_000_000),
                             ("deepseek-v4-pro", 0, 1_000_000, 1_000_000)):
            await db.execute(
                "INSERT INTO model_calls (conversation_id, model, input_tokens, "
                "output_tokens, cache_hit, cache_miss) VALUES (NULL, ?, ?, ?, ?, ?)",
                (m, ch + cm, o, ch, cm))
        await db.commit()
    finally:
        await db.close()
    r = await client.get("/api/logs/costs")
    w = r.json()["windows"]["all"]
    # flash: 0.14 + 0.28; pro: 0.435 + 0.87
    assert w["by_model"]["deepseek-v4-flash"]["cost_usd"] == pytest.approx(0.42)
    assert w["by_model"]["deepseek-v4-pro"]["cost_usd"] == pytest.approx(1.305)
    assert w["cost_usd"] == pytest.approx(1.725)
