"""Runtime model switch: /api/model over the provider registry (enabled-list
validation, persistence), gateway resolution (explicit pin > default), and
per-model cost pricing from the catalogue. Offline."""
import httpx
import pytest

from backend import providers
from backend.agent import model as model_mod
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db, set_state
from backend.main import app
from backend.memory import ensure_memory_seeds

FLASH = f"deepseek/{settings.model_name}"


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
        r = await c.post("/api/auth/login",
                         json={"username": "operator", "password": "hunter2"})
        assert r.status_code == 200
        yield c


async def test_switch_api_and_persistence(client):
    r = await client.get("/api/model")
    body = r.json()
    assert {k: body[k] for k in ("active", "default", "choices")} == {
        "active": FLASH, "default": FLASH, "choices": [FLASH]}

    r = await client.put("/api/model", json={"model": "gpt-9"})
    assert r.status_code == 400
    # a catalogued but not-enabled model is refused too: the switcher offers
    # only what the operator switched on
    r = await client.put("/api/model", json={"model": "deepseek-v4-pro"})
    assert r.status_code == 400

    # enable a second model, then switch to it by its bare id
    providers.update_model("deepseek", "deepseek-v4-pro", enabled=True)
    r = await client.put("/api/model", json={"model": "deepseek-v4-pro"})
    assert r.status_code == 200
    assert r.json()["active"] == "deepseek/deepseek-v4-pro"
    # persisted on disk: the registry reads it back fresh
    assert providers.default_model() == "deepseek/deepseek-v4-pro"
    # the old default stays in the picker instead of vanishing with the switch
    assert FLASH in r.json()["choices"]

    r = await client.put("/api/model", json={"model": FLASH})
    assert r.json()["active"] == FLASH


async def test_legacy_override_adopted_once(client):
    db = await get_db()
    try:
        await set_state(db, "model_override", "deepseek-v4-pro")
        await db.commit()
    finally:
        await db.close()
    await providers.migrate_legacy_override()
    assert providers.default_model() == "deepseek/deepseek-v4-pro"
    db = await get_db()
    try:
        from backend.db import get_state
        assert await get_state(db, "model_override") is None
    finally:
        await db.close()


async def test_gateway_resolves_default(tmp_env, monkeypatch):
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

    assert await run() == settings.model_name
    providers.update_model("deepseek", "deepseek-v4-pro", default=True)
    assert await run() == "deepseek-v4-pro"
    # an explicit per-call pin (agent model) beats the default
    assert await run(model_name="llama3:8b") == "llama3:8b"
    assert await run(model_name="deepseek/deepseek-flash") == "deepseek-flash"


async def test_costs_priced_per_model(client):
    db = await get_db()
    try:
        for m, ch, cm, o in (("deepseek-flash", 0, 1_000_000, 1_000_000),
                             ("deepseek-v4-flash", 0, 1_000_000, 1_000_000),
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
    # priced from the catalogue: flash 0.15 + 0.60, v4 flash (deprecated
    # alias, same price) 0.15 + 0.60, v4 pro 0.435 + 0.87
    assert w["by_model"]["deepseek-flash"]["cost_usd"] == pytest.approx(0.75)
    assert w["by_model"]["deepseek-v4-flash"]["cost_usd"] == pytest.approx(0.75)
    assert w["by_model"]["deepseek-v4-pro"]["cost_usd"] == pytest.approx(1.305)
    assert w["cost_usd"] == pytest.approx(2.805)


async def test_model_options_carry_labels(client):
    """Every choice comes back with a catalogue label and its provider as the
    blurb — the GUI renders these instead of hardcoding model names."""
    providers.update_model("deepseek", "deepseek-v4-pro", enabled=True)
    body = (await client.get("/api/model")).json()
    opts = {o["id"]: o for o in body["options"]}
    assert set(opts) == set(body["choices"])
    for c in body["choices"]:
        assert opts[c]["label"] and opts[c]["blurb"] == "DeepSeek"
    assert opts[FLASH]["label"] == providers.model_info(
        "deepseek", settings.model_name)["label"]


async def test_costs_by_provider_id_and_unpriced(client):
    """Rows ledgered as provider/model price from that provider; a catalogued
    model without prices costs 0 and says so instead of borrowing DeepSeek's."""
    unpriced = next((p["id"], m["id"]) for p in
                    providers.catalog()["providers"].values()
                    for m in p["models"] if m.get("price_in") is None)
    db = await get_db()
    try:
        for m in ("anthropic/claude-sonnet-5", "/".join(unpriced)):
            await db.execute(
                "INSERT INTO model_calls (conversation_id, model, input_tokens, "
                "output_tokens, cache_hit, cache_miss) VALUES (NULL, ?, ?, ?, ?, ?)",
                (m, 1_000_000, 1_000_000, 0, 1_000_000))
        await db.commit()
    finally:
        await db.close()
    w = (await client.get("/api/logs/costs")).json()["windows"]["all"]["by_model"]
    son = providers.model_info("anthropic", "claude-sonnet-5")
    assert w["anthropic/claude-sonnet-5"]["cost_usd"] == pytest.approx(
        son["price_in"] + son["price_out"])
    assert w["/".join(unpriced)] == {"calls": 1, "cost_usd": 0.0, "priced": False}
