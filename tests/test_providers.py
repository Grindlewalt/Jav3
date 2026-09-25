"""Provider registry + its control-plane API (v2.2 contract): catalogue and
operator override, persisted state, keys in the secrets store and never in a
response or a log line, the deepseek env fallback, the base_url allowlist
growing with enabled providers, the /test probe per wire kind (stubbed httpx
transport), model listing/default, and provider/model id resolution through
chat + schedules. Offline."""
import json
import logging

import httpx
import pytest

from backend import providers, secrets
from backend.agent import model as model_mod
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds

OPENAI_KEY = "sk-openai-test-0123456789"


@pytest.fixture
async def client(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "")
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


@pytest.fixture
def stub_http(monkeypatch):
    """Route the /test probe through a scripted transport. `routes` maps
    (method, url-without-query) -> httpx.Response; everything is recorded."""
    seen: list[httpx.Request] = []
    routes: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url).split("?")[0]
        resp = routes.get((request.method, url))
        return resp if resp is not None else httpx.Response(404, text="nope")

    monkeypatch.setattr(providers, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    return seen, routes


# --- registry + state ---------------------------------------------------------

def test_catalogue_shape_and_aliases(tmp_env):
    cat = providers.catalog()
    assert cat["source"].startswith("models.dev")
    ids = set(cat["providers"])
    for seed in ("deepseek", "openai", "anthropic", "google", "openrouter", "groq",
                 "mistral", "xai", "togetherai", "fireworks-ai", "ollama", "lmstudio"):
        assert seed in ids
    assert {p["kind"] for p in cat["providers"].values()} <= set(providers.KINDS)
    # the brief's seed names still resolve
    assert providers.provider("together")["id"] == "togetherai"
    assert providers.split_id("fireworks/some-model") == ("fireworks-ai", "some-model")


def test_operator_override_merges_on_top(tmp_env):
    (tmp_env / "providers.json").write_text(json.dumps({"providers": [
        {"id": "deepseek", "models": [{"id": "deepseek-flash", "price_in": 9.0}]},
        {"id": "mybox", "label": "My box", "kind": "openai",
         "base_url": "http://10.0.0.9:8000/v1", "auth": "none",
         "models": [{"id": "qwen", "label": "Qwen"}]},
        {"id": "broken", "kind": "telepathy", "base_url": "x"}]}))
    cat = providers.catalog()
    assert cat["source"].endswith("+ local overrides")
    assert providers.model_info("deepseek", "deepseek-flash")["price_in"] == 9.0
    assert providers.model_info("deepseek", "deepseek-flash")["price_out"] == 0.6
    assert providers.provider("mybox")["label"] == "My box"
    assert not providers.is_provider("broken")      # an invalid kind is ignored


def test_id_resolution(tmp_env):
    assert providers.split_id("deepseek-flash") == (None, "deepseek-flash")
    assert providers.canonical("deepseek-flash") == "deepseek/deepseek-flash"
    assert providers.split_id("openrouter/anthropic/claude-x") == \
        ("openrouter", "anthropic/claude-x")
    # an unknown prefix is part of a bare id, not a provider
    assert providers.canonical("library/llama3") == "deepseek/library/llama3"
    providers.set_key("openai", OPENAI_KEY)
    providers.update_model("openai", "gpt-4.1", default=True)
    # bare ids follow the DEFAULT model's provider
    assert providers.canonical("gpt-4.1-mini") == "openai/gpt-4.1-mini"


def test_defaults_and_state_persist(tmp_env):
    views = {p["id"]: p for p in providers.list_providers(include_models=False)}
    assert views["deepseek"]["enabled"] is True
    assert views["openai"]["enabled"] is False
    assert "models" not in views["openai"]
    providers.update_provider("openai", enabled=True)
    st = json.loads((settings.data_dir / "providers_state.json").read_text())
    assert st["providers"]["openai"]["enabled"] is True


def test_keys_live_in_the_secrets_store_unbound(tmp_env):
    providers.set_key("openai", OPENAI_KEY)
    assert secrets.load()["PROVIDER_OPENAI_API_KEY"] == OPENAI_KEY
    assert secrets.hosts_for("PROVIDER_OPENAI_API_KEY") == []   # never web-substitutable
    assert providers.get_provider("openai")["enabled"] is True  # first key switches it on
    providers.set_key("openai", "")
    assert "PROVIDER_OPENAI_API_KEY" not in secrets.load()
    with pytest.raises(providers.ProviderError):
        providers.set_key("openai", "two words")


def test_provider_keys_are_hidden_from_the_agents_secret_index(tmp_env):
    from backend.memory import secrets_index
    providers.set_key("openai", OPENAI_KEY)
    secrets.save({**secrets._load_raw(), "WEATHER_KEY": "w-123456"})
    idx = secrets_index()
    assert "WEATHER_KEY" in idx and "PROVIDER_OPENAI_API_KEY" not in idx


def test_first_key_respects_an_explicit_off(tmp_env):
    providers.update_provider("groq", enabled=False)
    providers.set_key("groq", "gsk-test-123456")
    assert providers.get_provider("groq")["enabled"] is False


def test_deepseek_env_fallback(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-env-deepseek-1234")
    v = providers.get_provider("deepseek")
    assert v["key_set"] is True and v["key_source"] == "env"
    assert providers.api_key("deepseek") == "sk-env-deepseek-1234"
    providers.set_key("deepseek", "sk-store-deepseek-9999")
    assert providers.get_provider("deepseek")["key_source"] == "store"
    assert providers.api_key("deepseek") == "sk-store-deepseek-9999"


def test_placeholder_base_url_must_be_set_before_enable(tmp_env):
    pid = next(p["id"] for p in providers.catalog()["providers"].values()
               if providers.needs_base_url(p["base_url"]))
    assert providers.get_provider(pid)["needs_base_url"] is True
    with pytest.raises(providers.ProviderError, match="base_url"):
        providers.update_provider(pid, enabled=True)
    providers.update_provider(pid, base_url="https://real.example.com/v1", enabled=True)
    assert providers.get_provider(pid)["enabled"] is True
    with pytest.raises(providers.ProviderError):
        providers.update_provider(pid, base_url="https://{STILL}.example.com")
    with pytest.raises(providers.ProviderError):
        providers.update_provider(pid, base_url="https://u:p@real.example.com")


# --- the allowlist / key attachment -------------------------------------------

def test_allowlist_grows_with_enabled_providers(tmp_env):
    url = "https://api.openai.com/v1"
    assert not providers.base_url_allowed(url)
    providers.set_key("openai", OPENAI_KEY)
    assert providers.base_url_allowed(url)
    assert model_mod.base_url_allowed(url)           # the gateway's check agrees
    providers.update_provider("openai", enabled=False)
    assert not providers.base_url_allowed(url)


def test_key_only_goes_to_its_own_endpoint(tmp_env):
    providers.set_key("openai", OPENAI_KEY)
    r = providers.resolve("openai/gpt-4.1")
    assert (r.base_url, r.key, r.kind) == ("https://api.openai.com/v1", OPENAI_KEY, "openai")
    # a guest naming the OpenAI endpoint explicitly gets OpenAI's key there...
    r = providers.resolve("gpt-4.1", base_url="https://api.openai.com/v1")
    assert r.key == OPENAI_KEY and r.provider == "openai"
    # ...a local allowlisted endpoint never gets a key, whatever the id says
    r = providers.resolve("openai/gpt-4.1", base_url="http://127.0.0.1:11434")
    assert r.key == "local" and r.provider is None
    # and a lookalike host is refused outright
    with pytest.raises(providers.ProviderError, match="refused model base_url"):
        providers.resolve("openai/gpt-4.1", base_url="https://api.openai.com.evil.xyz/v1")
    assert "sk-" not in repr(r)                      # the key is kept out of reprs


def test_disabled_or_keyless_provider_refuses(tmp_env):
    with pytest.raises(providers.ProviderError, match="disabled"):
        providers.resolve("openai/gpt-4.1")
    providers.update_provider("openai", enabled=True)
    assert "no API key" in providers.resolve("openai/gpt-4.1").key_error


# --- HTTP API -----------------------------------------------------------------

async def test_api_never_returns_or_logs_a_key(client, stub_http, caplog):
    seen, routes = stub_http
    routes[("GET", "https://api.openai.com/v1/models")] = httpx.Response(
        401, json={"error": f"bad key {OPENAI_KEY}"})
    caplog.set_level(logging.DEBUG)
    r = await client.put("/api/providers/openai", json={"api_key": OPENAI_KEY})
    assert r.status_code == 200 and r.json()["key_set"] is True
    bodies = [r.text, (await client.get("/api/providers")).text,
              (await client.get("/api/providers/openai")).text,
              (await client.get("/api/models")).text]
    t = await client.post("/api/providers/openai/test")
    bodies.append(t.text)
    assert t.json()["ok"] is False and "401" in t.json()["detail"]
    for b in bodies:
        assert OPENAI_KEY not in b
    assert OPENAI_KEY not in caplog.text
    # the probe itself did carry it, to the provider's own host
    assert seen[0].headers["authorization"] == f"Bearer {OPENAI_KEY}"


async def test_list_payload_contract(client):
    body = (await client.get("/api/providers")).json()
    assert body["catalog_source"].startswith("models.dev")
    ds = next(p for p in body["providers"] if p["id"] == "deepseek")
    for k in ("id", "label", "kind", "base_url", "key_set", "enabled", "models"):
        assert k in ds
    m = next(m for m in ds["models"] if m["id"] == settings.model_name)
    assert {k: m[k] for k in ("enabled", "default")} == {"enabled": True, "default": True}
    for k in ("label", "ctx", "price_in", "price_out"):
        assert k in m
    slim = (await client.get("/api/providers?models=0")).json()["providers"]
    assert all("models" not in p for p in slim)
    assert (await client.get("/api/providers/nope")).status_code == 404


async def test_base_url_null_resets(client):
    r = await client.put("/api/providers/lmstudio",
                         json={"base_url": "http://192.168.1.5:1234/v1"})
    assert r.json()["base_url"] == "http://192.168.1.5:1234/v1"
    r = await client.put("/api/providers/lmstudio", json={"enabled": True})
    assert r.json()["base_url"] == "http://192.168.1.5:1234/v1"   # omitted = kept
    r = await client.put("/api/providers/lmstudio", json={"base_url": None})
    assert r.json()["base_url"] == providers.provider("lmstudio")["base_url"]
    assert (await client.put("/api/providers/lmstudio",
                             json={"base_url": "ftp://x"})).status_code == 400


async def test_models_listing_default_and_slash_ids(client):
    await client.put("/api/providers/openrouter", json={"api_key": "sk-or-test-123456"})
    mid = next(m["id"] for m in providers.provider("openrouter")["models"] if "/" in m["id"])
    enc = mid.replace("/", "%2F")
    r = await client.put(f"/api/providers/openrouter/models/{enc}", json={"enabled": True})
    assert r.status_code == 200 and r.json()["model"]["enabled"] is True
    listing = (await client.get("/api/models")).json()
    ids = [m["id"] for m in listing["models"]]
    assert f"openrouter/{mid}" in ids and listing["default"] == f"deepseek/{settings.model_name}"
    item = next(m for m in listing["models"] if m["id"] == f"openrouter/{mid}")
    assert item["provider"] == "openrouter" and item["provider_label"] == "OpenRouter"

    # {default: true}: new default enabled, old default off-as-default but kept
    r = await client.put(f"/api/providers/openrouter/models/{mid}", json={"default": True})
    assert r.json()["default"] == f"openrouter/{mid}"
    flash = next(m for m in (await client.get("/api/providers/deepseek")).json()["models"]
                 if m["id"] == settings.model_name)
    assert flash["default"] is False and flash["enabled"] is True
    assert (await client.get("/api/model")).json()["active"] == f"openrouter/{mid}"
    # the default can't be disabled, nor its provider
    assert (await client.put(f"/api/providers/openrouter/models/{mid}",
                             json={"enabled": False})).status_code == 400
    assert (await client.put("/api/providers/openrouter",
                             json={"enabled": False})).status_code == 400
    assert (await client.put("/api/providers/openrouter/models/not-a-model",
                             json={"enabled": True})).status_code == 400


async def test_control_plane_is_cookie_only(tmp_env):
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        for method, url in (("GET", "/api/providers"), ("GET", "/api/models"),
                            ("PUT", "/api/providers/openai"),
                            ("POST", "/api/providers/openai/test")):
            r = await c.request(method, url, json={},
                                headers={"Authorization": "Bearer jav3_whatever"})
            assert r.status_code == 401, url


# --- the /test probe per kind -------------------------------------------------

async def test_probe_openai_lists_and_records_discovered(client, stub_http):
    seen, routes = stub_http
    routes[("GET", "https://api.openai.com/v1/models")] = httpx.Response(
        200, json={"data": [{"id": "gpt-4.1"}, {"id": "gpt-brand-new"}]})
    await client.put("/api/providers/openai", json={"api_key": OPENAI_KEY})
    res = (await client.post("/api/providers/openai/test")).json()
    assert res == {"ok": True, "detail": "key accepted, 2 models listed",
                   "models_found": ["gpt-4.1", "gpt-brand-new"]}
    models = {m["id"]: m for m in providers.get_provider("openai")["models"]}
    assert models["gpt-brand-new"]["discovered"] is True
    assert models["gpt-brand-new"]["enabled"] is False
    providers.update_model("openai", "gpt-brand-new", enabled=True)   # now pickable


async def test_probe_falls_back_to_one_token_on_404(client, stub_http):
    seen, routes = stub_http
    routes[("POST", "https://api.groq.com/openai/v1/chat/completions")] = \
        httpx.Response(200, json={"choices": []})
    await client.put("/api/providers/groq", json={"api_key": "gsk-test-123456"})
    res = (await client.post("/api/providers/groq/test")).json()
    assert res["ok"] is True and "1-token" in res["detail"]
    body = json.loads(seen[-1].content)
    assert body["max_tokens"] == 1


async def test_probe_anthropic(client, stub_http):
    seen, routes = stub_http
    routes[("GET", "https://api.anthropic.com/v1/models")] = httpx.Response(
        200, json={"data": [{"id": "claude-sonnet-5"}], "has_more": False})
    res = (await client.post("/api/providers/anthropic/test",
                             json={"api_key": "sk-ant-inline-123456"})).json()
    assert res["ok"] is True and res["models_found"] == ["claude-sonnet-5"]
    assert seen[0].headers["x-api-key"] == "sk-ant-inline-123456"
    assert seen[0].headers["anthropic-version"] == providers.ANTHROPIC_VERSION
    # an inline key tests without being stored
    assert providers.get_provider("anthropic")["key_set"] is False


async def test_probe_anthropic_compatible_without_listing(client, stub_http):
    seen, routes = stub_http
    p = providers.provider("minimax")
    assert p["kind"] == "anthropic" and p["lists_models"] is False
    routes[("POST", f"{p['base_url']}/messages")] = httpx.Response(200, json={})
    res = (await client.post("/api/providers/minimax/test",
                             json={"api_key": "mm-test-123456"})).json()
    assert res["ok"] is True
    assert [r.method for r in seen] == ["POST"]


async def test_probe_google(client, stub_http):
    seen, routes = stub_http
    routes[("GET", "https://generativelanguage.googleapis.com/v1beta/models")] = \
        httpx.Response(200, json={"models": [
            {"name": "models/gemini-2.5-flash",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-004",
             "supportedGenerationMethods": ["embedContent"]}]})
    res = (await client.post("/api/providers/google/test",
                             json={"api_key": "AIza-test-123456"})).json()
    assert res["models_found"] == ["gemini-2.5-flash"]
    assert seen[0].headers["x-goog-api-key"] == "AIza-test-123456"
    assert "key=" not in str(seen[0].url)


async def test_probe_ollama_needs_no_key(client, stub_http):
    seen, routes = stub_http
    routes[("GET", "http://localhost:11434/api/tags")] = httpx.Response(
        200, json={"models": [{"name": "qwen3:4b"}, {"name": "llama3:8b"}]})
    res = (await client.post("/api/providers/ollama/test")).json()
    assert res["ok"] is True and res["models_found"] == ["qwen3:4b", "llama3:8b"]
    assert "authorization" not in seen[0].headers
    models = [m["id"] for m in providers.get_provider("ollama")["models"]]
    assert models == ["qwen3:4b", "llama3:8b"]


async def test_probe_openrouter_validates_via_key_endpoint(client, stub_http):
    seen, routes = stub_http
    routes[("GET", "https://openrouter.ai/api/v1/key")] = httpx.Response(401, json={})
    await client.put("/api/providers/openrouter", json={"api_key": "sk-or-bad-123456"})
    res = (await client.post("/api/providers/openrouter/test")).json()
    assert res["ok"] is False
    assert [str(r.url) for r in seen] == ["https://openrouter.ai/api/v1/key"]


async def test_probe_adhoc_base_url_never_gets_the_stored_key(client, stub_http):
    seen, _ = stub_http
    await client.put("/api/providers/openai", json={"api_key": OPENAI_KEY})
    res = (await client.post("/api/providers/openai/test",
                             json={"base_url": "https://attacker.example/v1"})).json()
    assert res == {"ok": False, "detail": "no API key to test", "models_found": []}
    assert seen == []


async def test_probe_unreachable(client, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(providers, "HTTP_TRANSPORT", httpx.MockTransport(boom))
    res = (await client.post("/api/providers/ollama/test")).json()
    assert res["ok"] is False and "could not reach localhost" in res["detail"]


# --- ids through chat / schedules --------------------------------------------

async def test_chat_pins_and_reports_the_resolved_model(client, monkeypatch):
    from backend import chat as chat_mod
    got = {}

    async def fake_turn(cid, system_prompt, history, tools=None, **kw):
        got["model_name"] = kw.get("model_name")
        yield {"type": "final", "content": "ok"}

    async def no_naming(*a, **k):
        return None

    monkeypatch.setattr(chat_mod, "guest_turn", fake_turn)
    monkeypatch.setattr(chat_mod, "_name_conversation", no_naming)
    bad = await client.post("/api/chat", json={"message": "hi", "model": "openai/gpt-4.1"})
    assert bad.status_code == 400                     # not enabled

    providers.set_key("openai", OPENAI_KEY)
    providers.update_model("openai", "gpt-4.1", enabled=True)
    r = await client.post("/api/chat", json={"message": "hi", "model": "openai/gpt-4.1"})
    events = [json.loads(line[5:]) for line in r.text.splitlines()
              if line.startswith("data:")]
    start = next(e for e in events if e["type"] == "start")
    assert start["model"] == "openai/gpt-4.1"
    assert got["model_name"] == "openai/gpt-4.1"
    cid = start["conversation_id"]
    task = chat_mod._active_turns.get(cid)
    if task:
        await task
    db = await get_db()
    try:
        async with db.execute("SELECT model FROM conversations WHERE id = ?", (cid,)) as cur:
            assert (await cur.fetchone())["model"] == "openai/gpt-4.1"
        async with db.execute("SELECT model FROM messages WHERE conversation_id = ? "
                              "AND role = 'assistant'", (cid,)) as cur:
            assert (await cur.fetchone())["model"] == "openai/gpt-4.1"
    finally:
        await db.close()

    # an unpinned chat reports the canonical default, not a bare id
    r = await client.post("/api/chat", json={"message": "hi", "confirm_peak": True})
    start = next(json.loads(line[5:]) for line in r.text.splitlines()
                 if line.startswith("data:") and '"start"' in line)
    assert start["model"] == f"deepseek/{settings.model_name}"
    task = chat_mod._active_turns.get(start["conversation_id"])
    if task:
        await task


async def test_peak_gate_skips_other_providers(client, monkeypatch):
    from backend import chat as chat_mod
    monkeypatch.setattr(chat_mod, "in_peak_window", lambda *a, **k: True)

    async def fake_turn(cid, system_prompt, history, tools=None, **kw):
        yield {"type": "final", "content": "ok"}

    async def no_naming(*a, **k):
        return None

    monkeypatch.setattr(chat_mod, "guest_turn", fake_turn)
    monkeypatch.setattr(chat_mod, "_name_conversation", no_naming)
    r = await client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 409                       # deepseek default: gated
    providers.set_key("openai", OPENAI_KEY)
    providers.update_model("openai", "gpt-4.1", enabled=True)
    r = await client.post("/api/chat", json={"message": "hi", "model": "openai/gpt-4.1"})
    assert r.status_code == 200
    for task in list(chat_mod._active_turns.values()):
        await task


async def test_schedule_model_is_validated_and_stored(client):
    body = {"name": "n", "task": "t", "model": "openai/gpt-4.1"}
    assert (await client.post("/api/schedules", json=body)).status_code == 400
    body["model"] = settings.model_name               # bare id -> default provider
    sid = (await client.post("/api/schedules", json=body)).json()["id"]
    rows = (await client.get("/api/schedules")).json()["schedules"]
    assert next(r for r in rows if r["id"] == sid)["model"] == \
        f"deepseek/{settings.model_name}"
