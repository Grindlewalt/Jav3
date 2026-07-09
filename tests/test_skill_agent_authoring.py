"""Skills form builder (server-side frontmatter) + agent prompt generator."""
import httpx
import pytest

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
            ("operator", hash_password("hunter2")),
        )
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


async def test_skill_fields_roundtrip(client):
    r = await client.post("/api/skills", json={"name": "Deploy Site"})
    slug = r.json()["slug"]
    r = await client.put(f"/api/skills/{slug}/fields", json={
        "description": "Deploy the static site",
        "when_to_use": "when the operator says ship it",
        "enabled": True,
        "body": "1. build\n2. rsync",
        "params": [{"name": "env", "type": "string",
                    "description": "target env", "required": True},
                   {"name": "", "type": "string", "description": "ignored"}],
    })
    assert r.status_code == 200
    r = await client.get(f"/api/skills/{slug}")
    f = r.json()["fields"]
    assert f["description"] == "Deploy the static site"
    assert f["enabled"] is True
    assert f["body"] == "1. build\n2. rsync"
    assert f["params"] == [{"name": "env", "type": "string",
                            "description": "target env", "required": True}]
    # the generated file is valid registry material with a real schema
    from backend.agent.tools import registry
    entry = next(e for e in registry.compile_registry() if e["name"] == slug)
    assert entry["kind"] == "skill"
    assert entry["parameters"]["required"] == ["env"]


async def test_new_skills_granted_by_default(client):
    await client.post("/api/skills", json={"name": "Fresh One"})
    r = await client.get("/api/skills")
    s = next(x for x in r.json()["skills"] if x["slug"] == "fresh-one")
    assert s["enabled"] is True


async def test_prompt_quiz_and_generate(client, monkeypatch):
    from backend import summarize

    calls = {"n": 0}

    async def fake_complete(system, user, temperature=0.3):
        calls["n"] += 1
        if "ONLY a JSON array" in system:
            return ('[{"question": "What tone?", "kind": "single", '
                    '"options": ["formal", "casual"]}, '
                    '{"question": "Anything to avoid?", "kind": "short", '
                    '"options": []}]')
        return "You are NewsBot. You gather headlines and summarize them."

    monkeypatch.setattr(summarize, "complete_text", fake_complete)
    r = await client.post("/api/agents/prompt-quiz",
                          json={"description": "a news gathering agent"})
    qs = r.json()["questions"]
    assert qs[0]["kind"] == "single" and qs[0]["options"] == ["formal", "casual"]
    assert qs[1]["kind"] == "short"

    r = await client.post("/api/agents/prompt-generate", json={
        "description": "a news gathering agent",
        "answers": [{"question": "What tone?", "answer": "casual"}]})
    assert r.json()["prompt"].startswith("You are NewsBot")


async def test_prompt_quiz_retries_bad_json(client, monkeypatch):
    from backend import summarize
    replies = iter(["sorry, here you go:",
                    '[{"question": "Scope?", "kind": "short", "options": []}]'])

    async def fake_complete(system, user, temperature=0.3):
        return next(replies)

    monkeypatch.setattr(summarize, "complete_text", fake_complete)
    r = await client.post("/api/agents/prompt-quiz", json={"description": "x"})
    assert r.status_code == 200
    assert r.json()["questions"][0]["question"] == "Scope?"
