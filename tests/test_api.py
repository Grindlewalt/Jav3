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
        yield c


async def test_login_required(client):
    r = await client.get("/api/projects")
    assert r.status_code == 401


async def test_login_and_project_flow(client):
    r = await client.post("/api/auth/login",
                          json={"username": "operator", "password": "wrong"})
    assert r.status_code == 401

    r = await client.post("/api/auth/login",
                          json={"username": "operator", "password": "hunter2"})
    assert r.status_code == 200

    r = await client.post("/api/projects",
                          json={"name": "Jarvis v3", "summary": "The agent itself."})
    assert r.status_code == 200
    assert r.json()["slug"] == "jarvis-v3"

    # duplicate slug rejected
    r = await client.post("/api/projects", json={"name": "Jarvis V3"})
    assert r.status_code == 409

    r = await client.post("/api/projects/jarvis-v3/load")
    assert r.status_code == 200

    r = await client.get("/api/debug/context")
    assert r.status_code == 200
    body = r.json()
    assert body["active_project"] == "jarvis-v3"
    assert "The agent itself." in body["system_prompt"]

    r = await client.post("/api/projects/unload")
    assert r.status_code == 200
    r = await client.get("/api/debug/context")
    assert r.json()["active_project"] is None
