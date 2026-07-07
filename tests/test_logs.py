"""Log viewer API: transcript timeline + stats that explain a token blow-up."""
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
        await db.execute("INSERT INTO conversations (id, kind, summary) VALUES (1, 'chat', 'Heavy chat')")
        await db.execute("INSERT INTO messages (conversation_id, role, content) VALUES (1, 'user', 'go')")
        for i in range(5):
            await db.execute(
                "INSERT INTO tool_calls (conversation_id, tool, args, result) VALUES (1, 'web_read', ?, ?)",
                ("{}", "x" * 6000))
        await db.execute("INSERT INTO messages (conversation_id, role, content) VALUES (1, 'assistant', 'done')")
        await db.execute(
            "INSERT INTO usage_log (conversation_id, input_tokens, output_tokens, cache_hit, cache_miss) "
            "VALUES (1, 1200000, 45000, 900000, 300000)")
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        yield c


async def test_conversations_list_carries_stats(client):
    r = await client.get("/api/logs/conversations")
    row = next(c for c in r.json()["conversations"] if c["id"] == 1)
    assert row["tool_calls"] == 5 and row["result_bytes"] == 30000
    assert row["input_tokens"] == 1200000 and row["output_tokens"] == 45000


async def test_transcript_timeline_and_histogram(client):
    r = await client.get("/api/logs/conversations/1")
    body = r.json()
    kinds = [t["kind"] for t in body["timeline"]]
    assert kinds[0] == "message" and "tool" in kinds and kinds[-1] == "message"
    assert body["stats"]["tool_calls"] == 5
    assert body["stats"]["input_tokens"] == 1200000
    hist = {h["tool"]: h for h in body["tool_histogram"]}
    assert hist["web_read"]["count"] == 5 and hist["web_read"]["bytes"] == 30000


async def test_transcript_404(client):
    assert (await client.get("/api/logs/conversations/999")).status_code == 404
