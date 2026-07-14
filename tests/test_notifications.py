"""Approval notification center — aggregates the three pending stores.

Read-only aggregation; tested against an authed client with an empty DB (no
pending anything) plus one seeded git request to prove it surfaces."""
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
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


async def test_notifications_empty(client):
    r = await client.get("/api/notifications")
    assert r.status_code == 200
    body = r.json()
    assert body == {"count": 0, "git": [], "staged": [], "schedules": []}


async def test_notifications_surfaces_pending_git(client):
    # seed a project + a pending git request
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO projects (slug, name, path) VALUES ('proj', 'Proj', 'proj')")
        await db.execute(
            "INSERT INTO git_requests (project_slug, message, status) "
            "VALUES ('proj', 'commit the thing', 'pending')")
        await db.commit()
    finally:
        await db.close()
    body = (await client.get("/api/notifications")).json()
    assert body["count"] == 1
    assert body["git"] and body["git"][0]["project"] == "proj"
    assert body["git"][0]["message"] == "commit the thing"


async def test_notifications_requires_auth():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/notifications")).status_code == 401
