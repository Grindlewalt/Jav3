"""Background chat execution: a turn survives its client going away, can be
re-attached to via GET /api/chat/{id}/stream, reports `running` in the
messages payload, and refuses a second concurrent message per conversation.

httpx's ASGITransport buffers streaming responses until the app finishes, so
these tests drive concurrent requests as asyncio tasks instead of reading a
live SSE stream mid-response."""
import asyncio
import contextlib

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


def _slow_turn(release: asyncio.Event, started: asyncio.Event, text: str):
    async def turn(db, cid, system_prompt, history, tools=None, **kw):
        started.set()
        await release.wait()
        yield {"type": "final", "content": text}
    return turn


async def _start_blocked_turn(client, monkeypatch, text="done"):
    """POST a chat message whose turn blocks on an event; return
    (post_task, conversation_id, release_event)."""
    from backend import chat as chat_mod
    release, started = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(chat_mod, "run_turn", _slow_turn(release, started, text))
    post = asyncio.create_task(client.post(
        "/api/chat", json={"message": "hi", "confirm_peak": True}))
    await asyncio.wait_for(started.wait(), 5)
    cid = max(chat_mod._active_turns)
    return post, cid, release


async def test_turn_survives_client_disconnect(client, monkeypatch):
    from backend import chat as chat_mod
    post, cid, release = await _start_blocked_turn(
        client, monkeypatch, "finished in background")
    r = await client.get(f"/api/conversations/{cid}/messages")
    assert r.json()["running"] is True
    # the client walks away mid-turn — only the tail dies, not the work
    post.cancel()
    with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
        await post
    release.set()
    task = chat_mod._active_turns.get(cid)
    if task:
        await task
    r = await client.get(f"/api/conversations/{cid}/messages")
    body = r.json()
    assert body["running"] is False
    assert body["messages"][-1]["role"] == "assistant"
    assert body["messages"][-1]["content"] == "finished in background"


async def test_resume_stream_attaches_to_running_turn(client, monkeypatch):
    post, cid, release = await _start_blocked_turn(
        client, monkeypatch, "the full reply")
    tail = asyncio.create_task(client.get(f"/api/chat/{cid}/stream"))
    await asyncio.sleep(0.05)   # let the tail subscribe before releasing
    release.set()
    r = await asyncio.wait_for(tail, 5)
    assert '"final"' in r.text and "the full reply" in r.text
    await asyncio.wait_for(post, 5)


async def test_resume_stream_idle_when_no_turn(client):
    r = await client.get("/api/chat/424242/stream")
    assert '"idle"' in r.text


async def test_second_message_while_running_409s(client, monkeypatch):
    from backend import chat as chat_mod
    post, cid, release = await _start_blocked_turn(client, monkeypatch, "ok")
    r = await client.post("/api/chat", json={
        "message": "again", "conversation_id": cid, "confirm_peak": True})
    assert r.status_code == 409
    assert r.json()["detail"] == "turn_in_progress"
    release.set()
    await asyncio.wait_for(post, 5)
    task = chat_mod._active_turns.get(cid)
    if task:
        await task
    # only the original exchange landed — the 409'd message was never inserted
    r = await client.get(f"/api/conversations/{cid}/messages")
    roles = [m["role"] for m in r.json()["messages"]]
    assert roles == ["user", "assistant"]