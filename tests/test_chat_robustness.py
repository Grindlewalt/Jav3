"""Chat turn robustness (M1, second hunt): the double-submit claim, failure
text and transcript, provider balance, publish-before-journal, guarded
cleanup, incognito visibility, SSE keepalive, delete-while-running and the
interrupted-request note.

httpx's ASGITransport buffers a streaming response until the app finishes, so
a live turn is driven as asyncio tasks (see test_background_chat.py)."""
import asyncio
import contextlib
import logging

import httpx
import pytest

from backend import chat as chat_mod
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


@pytest.fixture
async def client(tmp_env, monkeypatch):
    await init_db()
    ensure_memory_seeds()

    async def no_naming(*a, **k):
        return None
    monkeypatch.setattr(chat_mod, "_name_conversation", no_naming)
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


def _blocked(release: asyncio.Event, started: asyncio.Event, text="done", seen=None):
    async def turn(cid, system_prompt, history, tools=None, **kw):
        if seen is not None:
            seen.append({"cid": cid, "history": history})
        started.set()
        await release.wait()
        yield {"type": "final", "content": text}
    return turn


def _raising(exc):
    async def turn(cid, system_prompt, history, tools=None, **kw):
        yield {"type": "token", "text": "working on it"}
        raise exc
    return turn


async def _settle(cid: int):
    task = chat_mod._active_turns.get(cid)
    if task:
        with contextlib.suppress(BaseException):
            await task


async def _first_conversation(client, monkeypatch, **body) -> int:
    """One finished turn, so a conversation exists; returns its id."""
    monkeypatch.setattr(chat_mod, "guest_turn", _blocked(
        done := asyncio.Event(), started := asyncio.Event(), "first"))
    post = asyncio.create_task(client.post("/api/chat", json={"message": "hi", **body}))
    await asyncio.wait_for(started.wait(), 5)
    cid = max(chat_mod._active_turns)
    done.set()
    await asyncio.wait_for(post, 5)
    await _settle(cid)
    return cid


async def _roles(client, cid):
    r = await client.get(f"/api/conversations/{cid}/messages")
    return [(m["role"], m["content"]) for m in r.json()["messages"]]


# --- ROBUST-07: the 409 guard is one synchronous claim ----------------------


async def test_double_submit_starts_one_turn(client, monkeypatch):
    cid = await _first_conversation(client, monkeypatch)
    release, started, seen = asyncio.Event(), asyncio.Event(), []
    monkeypatch.setattr(chat_mod, "guest_turn", _blocked(release, started, "x", seen))
    body = {"message": "again", "conversation_id": cid}
    a = asyncio.create_task(client.post("/api/chat", json=body))
    b = asyncio.create_task(client.post("/api/chat", json=body))
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.3)
    refused = [t for t in (a, b) if t.done()]
    assert len(refused) == 1
    assert refused[0].result().status_code == 409
    assert refused[0].result().json()["detail"] == "turn_in_progress"
    release.set()
    await asyncio.gather(a, b)
    await _settle(cid)
    assert len(seen) == 1                       # one turn, not two
    roles = [r for r, _ in await _roles(client, cid)]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert not chat_mod._posting                # the claim does not leak


async def test_claim_released_when_the_post_fails(client, monkeypatch):
    r = await client.post("/api/chat", json={"message": "x", "conversation_id": 9999})
    assert r.status_code == 404
    assert not chat_mod._posting


async def test_post_into_a_live_agent_node_is_refused(client, monkeypatch):
    cid = await _first_conversation(client, monkeypatch)
    from backend import agents_run
    monkeypatch.setitem(agents_run._active_runs, cid, object())
    r = await client.post("/api/chat", json={"message": "hey", "conversation_id": cid})
    assert r.status_code == 409
