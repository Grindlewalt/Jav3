import httpx
import pytest

from backend.auth import hash_password
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


async def test_agent_crud(client):
    r = await client.post("/api/agents", json={"name": "Research Helper"})
    assert r.status_code == 200 and r.json()["slug"] == "research-helper"
    assert (await client.post("/api/agents", json={"name": "Research Helper"})).status_code == 409

    r = await client.get("/api/agents/research-helper")
    a = r.json()
    # exclusion model: nothing excluded by default
    assert a["context_exclude"] == [] and a["tools_exclude"] == []
    assert a["model"] == "" and a["own_memory"] is False
    assert "Research Helper" in a["prompt"]

    a.update({
        "model": "qwen2.5:14b",
        "base_url": "http://localhost:11434/v1",
        "context_exclude": ["user.md"],
        "own_memory": True,
        "prompt": "You research things and report back.",
    })
    r = await client.put("/api/agents/research-helper", json=a)
    assert r.status_code == 200

    r = await client.get("/api/agents/research-helper")
    b = r.json()
    assert b["model"] == "qwen2.5:14b"
    assert b["context_exclude"] == ["user.md"]
    assert b["own_memory"] is True
    assert b["prompt"] == "You research things and report back."

    r = await client.get("/api/agents")
    assert r.json()["agents"][0]["model"] == "qwen2.5:14b"

    assert (await client.delete("/api/agents/research-helper")).status_code == 200
    assert (await client.get("/api/agents/research-helper")).status_code == 404
