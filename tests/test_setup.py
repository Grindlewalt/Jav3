"""First-run setup: a door that exists only while no user does.

Checked here: the status flip, exactly-once creation (including two setups
racing), the session cookie it hands back, the provider step with PR1's
backend.providers both present (faked) and absent, the throttle on a closed
door, and that the same-origin exemption covers /api/setup and nothing else.
"""
import asyncio
import io
import json
import sys
import types

import httpx
import pytest

from backend import auth, setup_api
from backend.auth import COOKIE_NAME, SameOriginMiddleware, user_from_token
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app

BASE = "http://jav3.lan:8000"
GOOD = {"username": "operator", "password": "correct horse"}


@pytest.fixture(autouse=True)
def _clean():
    auth._failures.clear()
    yield
    auth._failures.clear()


@pytest.fixture
def no_providers(monkeypatch):
    """PR1 not merged: `from . import providers` fails."""
    monkeypatch.setattr(setup_api, "_providers_mod", lambda: None)


@pytest.fixture
def fake_providers(monkeypatch):
    """PR1's API as the coordinator relayed it: sync list/get/set_key/
    update_*, unknown ids raise ValueError, get_provider model ids bare."""
    calls = []
    rows = {
        "zeta": {"id": "zeta", "label": "Zeta", "kind": "openai",
                 "base_url": "https://api.zeta.test", "needs_key": True},
        "deepseek": {"id": "deepseek", "label": "DeepSeek", "kind": "openai",
                     "base_url": "https://api.example.test", "needs_key": True,
                     "models": [{"id": "flash"}, {"id": "pro"}]},
        "ollama": {"id": "ollama", "label": "Ollama", "kind": "ollama",
                   "base_url": "http://localhost:11434", "needs_key": False,
                   "models": [{"id": "llama"}]},
        "azure": {"id": "azure", "label": "Azure", "kind": "openai",
                  "base_url": "https://{RESOURCE}.example.test", "needs_key": True,
                  "needs_base_url": True},
    }

    def get_provider(pid):
        if pid not in rows:
            raise ValueError(f"unknown provider {pid}")
        return rows[pid]
    mod = types.SimpleNamespace(
        set_key=lambda pid, key: calls.append(("set_key", pid, key)),
        update_provider=lambda pid, **kw: calls.append(("update_provider", pid, kw)),
        update_model=lambda pid, model, **kw: calls.append(("update_model", pid, model, kw)),
        get_provider=get_provider,
        list_providers=lambda: list(rows.values()),
    )
    monkeypatch.setattr(setup_api, "_providers_mod", lambda: mod)
    return calls


@pytest.fixture
async def client(tmp_env):
    await init_db()          # the app lifespan's job; ASGITransport skips it
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url=BASE) as c:
        yield c


async def _count_users() -> int:
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            return (await cur.fetchone())[0]
    finally:
        await db.close()


async def test_status_flips_once_a_user_exists(client, no_providers):
    assert (await client.get("/api/setup/status")).json() == {"needed": True}
    r = await client.post("/api/setup", json=GOOD)
    assert r.status_code == 200, r.text
    assert (await client.get("/api/setup/status")).json() == {"needed": False}


async def test_setup_creates_the_user_and_logs_in(client, no_providers):
    r = await client.post("/api/setup", json=GOOD)
    assert r.status_code == 200
    assert r.json()["username"] == "operator"
    user = user_from_token(client.cookies.get(COOKIE_NAME))
    assert user and user["username"] == "operator"
    me = await client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["username"] == "operator"
    # and the password actually works on the ordinary front door
    r = await client.post("/api/auth/login", json=GOOD)
    assert r.status_code == 200


async def test_setup_refuses_once_done(client, no_providers):
    assert (await client.post("/api/setup", json=GOOD)).status_code == 200
    r = await client.post("/api/setup", json={"username": "mallory",
                                               "password": "password123"})
    assert r.status_code == 409
    assert (await client.get("/api/setup/providers")).status_code == 409
    r = await client.post("/api/setup/test", json={"provider": "ollama"})
    assert r.status_code == 409
    assert await _count_users() == 1


async def test_two_racing_setups_cannot_both_win(client, no_providers):
    rs = await asyncio.gather(*[
        client.post("/api/setup", json={"username": f"u{i}", "password": "password123"})
        for i in range(4)])
    codes = sorted(r.status_code for r in rs)
    assert codes.count(200) == 1, codes
    assert all(c == 409 for c in codes if c != 200)
    assert await _count_users() == 1


async def test_create_first_user_is_atomic_under_gather(tmp_env):
    await init_db()
    res = await asyncio.gather(
        *[setup_api.create_first_user(f"u{i}", "password123") for i in range(5)],
        return_exceptions=True)
    wins = [r for r in res if isinstance(r, int)]
    losses = [r for r in res if isinstance(r, setup_api.SetupError)]
    assert len(wins) == 1 and len(losses) == 4
    assert all(e.status == 409 for e in losses)


async def test_bad_credentials_are_refused_without_creating(client, no_providers):
    r = await client.post("/api/setup", json={"username": "op", "password": "short"})
    assert r.status_code == 400
    r = await client.post("/api/setup", json={"username": "bad name!",
                                               "password": "password123"})
    assert r.status_code == 400
    assert await _count_users() == 0


async def test_provider_missing_key_is_refused_before_the_user_exists(client, no_providers):
    r = await client.post("/api/setup", json={**GOOD, "provider": "openai"})
    assert r.status_code == 400
    assert await _count_users() == 0, "a bad provider step must not half-finish setup"


async def test_provider_step_without_pr1_stores_the_key_in_secrets(client, no_providers):
    r = await client.post("/api/setup", json={**GOOD, "provider": "deepseek",
                                              "api_key": "sk-test-123456"})
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == {"stored_as": "secrets", "default": None}
    stored = json.loads(settings.secrets_path.read_text())
    assert stored["PROVIDER_DEEPSEEK_API_KEY"] == "sk-test-123456"
    assert oct(settings.secrets_path.stat().st_mode & 0o777) == "0o600"


async def test_provider_step_with_pr1_sets_key_enables_and_defaults(client, fake_providers):
    r = await client.post("/api/setup", json={**GOOD, "provider": "deepseek",
                                              "api_key": "sk-abc"})
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == {"stored_as": "providers", "default": "deepseek/flash"}
    assert ("set_key", "deepseek", "sk-abc") in fake_providers
    assert ("update_provider", "deepseek",
            {"base_url": None, "enabled": True}) in fake_providers
    assert ("update_model", "deepseek", "flash",
            {"enabled": True, "default": True}) in fake_providers
    assert not settings.secrets_path.exists()


async def test_local_provider_needs_no_key_and_keeps_base_url(client, fake_providers):
    r = await client.post("/api/setup", json={**GOOD, "provider": "ollama",
                                              "base_url": "http://10.1.2.3:11434"})
    assert r.status_code == 200, r.text
    assert not any(c[0] == "set_key" for c in fake_providers)
    assert ("update_provider", "ollama",
            {"enabled": True, "base_url": "http://10.1.2.3:11434"}) in fake_providers


async def test_catalogue_uses_pr1_or_falls_back(client, monkeypatch):
    monkeypatch.setattr(setup_api, "_providers_mod", lambda: None)
    ids = [p["id"] for p in (await client.get("/api/setup/providers")).json()["providers"]]
    assert ids == ["deepseek", "openai", "anthropic", "google", "openrouter", "ollama"]
    ollama = [p for p in setup_api.catalogue() if p["id"] == "ollama"][0]
    assert ollama["needs_key"] is False


async def test_catalogue_from_pr1(client, fake_providers):
    rows = (await client.get("/api/setup/providers")).json()["providers"]
    # Popular first (deepseek before ollama), the rest after; no model lists
    assert [p["id"] for p in rows] == ["deepseek", "ollama", "zeta", "azure"]
    assert all("models" not in p for p in rows)
    by = {p["id"]: p for p in rows}
    assert by["ollama"]["needs_key"] is False and by["deepseek"]["needs_key"] is True
    assert by["azure"]["needs_base_url"] is True


async def test_pr1_unknown_id_and_required_base_url(client, fake_providers):
    r = await client.post("/api/setup", json={**GOOD, "provider": "nope", "api_key": "k"})
    assert r.status_code == 400
    r = await client.post("/api/setup", json={**GOOD, "provider": "azure", "api_key": "k"})
    assert r.status_code == 400 and "base URL" in r.json()["detail"]
    assert await _count_users() == 0


async def test_pr1_async_tester_gets_the_candidate_key(client, fake_providers):
    mod = setup_api._providers_mod()
    seen = {}

    async def tester(pid, api_key=None, base_url=None):
        seen.update(pid=pid, key=api_key, base=base_url)
        return {"ok": True, "detail": "ok", "models_found": ["flash", "pro"]}
    mod.test_provider = tester
    r = await client.post("/api/setup/test", json={"provider": "deepseek", "api_key": "sk"})
    assert r.json()["models_found"] == ["flash", "pro"]
    assert seen == {"pid": "deepseek", "key": "sk", "base": None}


async def test_test_route_uses_the_probe(client, no_providers, monkeypatch):
    seen = {}

    async def probe(entry, key, base):
        seen.update(id=entry["id"], key=key, base=base)
        return {"ok": True, "detail": "ok", "models_found": ["a", "b"]}
    monkeypatch.setattr(setup_api, "_probe", probe)
    r = await client.post("/api/setup/test", json={"provider": "openai", "api_key": "k"})
    assert r.json() == {"ok": True, "detail": "ok", "models_found": ["a", "b"]}
    assert seen == {"id": "openai", "key": "k", "base": ""}
    r = await client.post("/api/setup/test", json={"provider": "nope"})
    assert r.status_code == 400
    r = await client.post("/api/setup/test",
                          json={"provider": "ollama", "base_url": "file:///etc/passwd"})
    assert r.status_code == 400


async def test_probe_parses_openai_and_ollama_shapes(monkeypatch):
    def handler(req: httpx.Request):
        if req.url.path.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": "llama3:8b"}]})
        if req.headers.get("authorization") != "Bearer good":
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})
    real = httpx.AsyncClient
    monkeypatch.setattr(setup_api.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    oa = {"id": "x", "kind": "openai", "base_url": "https://api.x.test/v1"}
    assert (await setup_api._probe(oa, "good", ""))["models_found"] == ["m1", "m2"]
    bad = await setup_api._probe(oa, "bad", "")
    assert bad["ok"] is False and "refused" in bad["detail"]
    ol = {"id": "ollama", "kind": "ollama", "base_url": "http://localhost:11434"}
    assert (await setup_api._probe(ol, "", ""))["models_found"] == ["llama3:8b"]


async def test_a_closed_door_is_throttled_like_login(client, no_providers, monkeypatch):
    assert (await client.post("/api/setup", json=GOOD)).status_code == 200
    slept = []

    async def fake_sleep(d):
        slept.append(d)
    monkeypatch.setattr(setup_api.asyncio, "sleep", fake_sleep)
    for _ in range(4):
        r = await client.post("/api/setup", json=GOOD)
        assert r.status_code == 409
    assert slept == [0.5, 1.0, 2.0], "first refusal free, then the login ladder"


async def test_cross_site_and_non_json_are_refused(client, no_providers):
    r = await client.post("/api/setup", json=GOOD,
                          headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    r = await client.post("/api/setup", content=json.dumps(GOOD),
                          headers={"content-type": "text/plain"})
    assert r.status_code in (415, 422)   # FastAPI refuses a non-JSON body first
    assert await _count_users() == 0


async def test_stale_cookie_does_not_wedge_setup(client, no_providers):
    """A leftover cookie from an earlier install plus a foreign-looking Origin
    (a reverse proxy name) must not 403 the one-time setup."""
    client.cookies.set(COOKIE_NAME, "stale")
    r = await client.post("/api/setup", json=GOOD,
                          headers={"origin": "https://proxy.example.test"})
    assert r.status_code == 200, r.text


def test_origin_exemption_is_path_exact():
    must = SameOriginMiddleware._must_check

    def scope(path):
        return {"type": "http", "method": "POST", "path": path}
    assert must(scope("/api/setup")) is False
    for p in ("/api/setup/test", "/api/setup/", "/api/setupx", "/api/setup/../chat"):
        assert must(scope(p)) is True, p


# ------------------------------------------------------------------- CLI ---

def _run_cli(monkeypatch, argv, stdin_text, tty=False):
    from backend import cli
    monkeypatch.setattr(sys, "argv", ["backend.cli", *argv])
    stdin = io.StringIO(stdin_text)
    stdin.isatty = lambda: tty
    monkeypatch.setattr(sys, "stdin", stdin)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    code = 0
    try:
        cli.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue()


def test_cli_setup_non_interactive(tmp_env, monkeypatch, no_providers):
    code, out = _run_cli(monkeypatch, [
        "setup", "--username", "op", "--password-stdin",
        "--provider", "deepseek", "--api-key-stdin", "--no-test"],
        "password123\nsk-cli-999999\n")
    assert code == 0, out
    assert "open" in out.lower() and f":{settings.lan_port}/" in out
    assert asyncio.run(_count_users()) == 1
    stored = json.loads(settings.secrets_path.read_text())
    assert stored["PROVIDER_DEEPSEEK_API_KEY"] == "sk-cli-999999"
    # idempotent: a second run refuses
    code, out = _run_cli(monkeypatch, ["setup", "--username", "op2",
                                       "--password-stdin"], "password123\n")
    assert code != 0
    assert asyncio.run(_count_users()) == 1


def test_cli_setup_add_user(tmp_env, monkeypatch, no_providers):
    asyncio.run(init_db())
    asyncio.run(setup_api.create_first_user("op", "password123"))
    code, out = _run_cli(monkeypatch, ["setup", "--add-user", "--username", "second",
                                       "--password-stdin"], "password456\n")
    assert code == 0, out
    assert asyncio.run(_count_users()) == 2


def test_cli_setup_interactive_menu(tmp_env, monkeypatch, no_providers):
    """The numbered menu over fed stdin: username, provider 6 (ollama, no
    key), accept the default base URL, skip the test."""
    from backend import cli
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "password123")
    monkeypatch.setattr(setup_api, "_probe", None)   # must not be reached
    code, out = _run_cli(monkeypatch, ["setup"], "op\n6\n\nn\n", tty=True)
    assert code == 0, out
    assert "1) DeepSeek" in out and "6) Ollama" in out
    assert asyncio.run(_count_users()) == 1


def test_cli_setup_interactive_skip_provider(tmp_env, monkeypatch, no_providers):
    from backend import cli
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "password123")
    code, out = _run_cli(monkeypatch, ["setup"], "op\n0\n", tty=True)
    assert code == 0, out
    assert not settings.secrets_path.exists()
