"""Jobs view: /api/jobs lists agent/scheduled/head conversations, never chats."""
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
                         ("operator", hash_password("pw")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "pw"})
        yield c


async def test_jobs_listing_filter_and_chat_split(client):
    db = await get_db()
    try:
        # explicit started_at offsets so newest-first ordering is deterministic
        rows = [("a chat", "chat", "-10 minutes", None),
                ("an agent run", "agent", "-9 minutes", None),
                ("a scheduled run", "scheduled", "-8 minutes", None),
                ("a research head", "head", "-7 minutes", "j1")]
        for summary, kind, offset, job_id in rows:
            await db.execute(
                "INSERT INTO conversations (summary, kind, started_at, job_id) "
                "VALUES (?, ?, datetime('now', ?), ?)",
                (summary, kind, offset, job_id))
        await db.commit()
    finally:
        await db.close()

    jobs = (await client.get("/api/jobs")).json()["jobs"]
    assert [j["summary"] for j in jobs] == [
        "a research head", "a scheduled run", "an agent run"]  # no chat, newest first
    for j in jobs:
        assert set(j) >= {"id", "kind", "summary", "project", "started_at",
                          "done", "job_id"}
        assert j["done"] is False  # none finished yet
    assert jobs[0]["job_id"] == "j1"

    # ?kind= filters; unknown kinds are rejected
    jobs = (await client.get("/api/jobs", params={"kind": "agent"})).json()["jobs"]
    assert [j["summary"] for j in jobs] == ["an agent run"]
    assert (await client.get("/api/jobs", params={"kind": "chat"})).status_code == 400

    # the chat sidebar shows only real chats
    convos = (await client.get("/api/conversations")).json()["conversations"]
    assert [c["summary"] for c in convos] == ["a chat"]


async def test_jobs_done_markers(client):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO conversations (summary, kind, job_id, rollup) "
            "VALUES ('done head', 'head', 'jd', 'rolled up')")
        cur = await db.execute(
            "INSERT INTO conversations (summary, kind) VALUES ('done agent', 'agent')")
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) "
            "VALUES (?, 'assistant', 'final answer')", (cur.lastrowid,))
        await db.execute(
            "INSERT INTO conversations (summary, kind) VALUES ('running agent', 'agent')")
        await db.commit()
    finally:
        await db.close()
    jobs = (await client.get("/api/jobs")).json()["jobs"]
    done = {j["summary"]: j["done"] for j in jobs}
    assert done == {"done head": True, "done agent": True, "running agent": False}
