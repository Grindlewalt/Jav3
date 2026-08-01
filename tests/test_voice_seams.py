"""Phase-0 seams for voice desktop mode: the interrupt-note hook (a barge-in
writes WHAT WAS ACTUALLY HEARD into the transcript instead of the bare stop
marker) and the rewrite_rules flag (voice turns must not let _enforce_rules
rewrite text that was already spoken aloud).

Same driving style as test_background_chat.py: turns are blocked on an event
and run as asyncio tasks; no model calls anywhere.
"""
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
    async def turn(cid, system_prompt, history, tools=None, **kw):
        started.set()
        await release.wait()
        yield {"type": "final", "content": text}
    return turn


async def _start_blocked_turn(client, monkeypatch, text="done"):
    from backend import chat as chat_mod
    release, started = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(chat_mod, "run_turn", _slow_turn(release, started, text))
    post = asyncio.create_task(client.post(
        "/api/chat", json={"message": "hi", "confirm_peak": True}))
    await asyncio.wait_for(started.wait(), 5)
    cid = max(chat_mod._active_turns)
    return post, cid, release


async def test_interrupt_note_replaces_marker(client, monkeypatch):
    """set_interrupt_note + cancel writes the annotated note (spoken prefix)
    to the transcript and publishes it as the final event — the voice
    barge-in path."""
    from backend import chat as chat_mod
    note = ('The capital of France is [— reply cut off here by the operator; '
            'nothing after this point was generated or heard]')
    post, cid, release = await _start_blocked_turn(client, monkeypatch, "never")

    chat_mod.set_interrupt_note(cid, note)
    task = chat_mod._active_turns[cid]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    r = await asyncio.wait_for(post, 5)
    # (the em-dash arrives JSON-escaped in the SSE frame, so match around it)
    assert "cut off here by the operator" in r.text
    assert chat_mod.INTERRUPTED_MARKER not in r.text

    body = (await client.get(f"/api/conversations/{cid}/messages")).json()
    assert body["messages"][-1]["role"] == "assistant"
    assert body["messages"][-1]["content"] == note
    # consumed, not leaked
    assert cid not in chat_mod._interrupt_notes
    release.set()  # harmless; turn already gone


async def test_stale_note_does_not_leak_into_completed_turn(client, monkeypatch):
    """A note set on a turn that then finishes normally must be swept by the
    turn's finally — the next interruption in that conversation must show the
    plain marker, not yesterday's annotation."""
    from backend import chat as chat_mod
    post, cid, release = await _start_blocked_turn(client, monkeypatch, "fine")
    chat_mod.set_interrupt_note(cid, "stale annotation")
    release.set()
    task = chat_mod._active_turns.get(cid)
    if task:
        await task
    r = await asyncio.wait_for(post, 5)
    assert "fine" in r.text and "stale annotation" not in r.text
    assert cid not in chat_mod._interrupt_notes
    body = (await client.get(f"/api/conversations/{cid}/messages")).json()
    assert body["messages"][-1]["content"] == "fine"


async def test_plain_stop_still_writes_marker(client, monkeypatch):
    """No note set → the GUI stop path is byte-for-byte what it was."""
    from backend import chat as chat_mod
    post, cid, release = await _start_blocked_turn(client, monkeypatch, "never")
    r = await client.post(f"/api/chat/{cid}/stop")
    assert r.json()["stopped"] is True
    task = chat_mod._active_turns.get(cid)
    if task:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    body = (await client.get(f"/api/conversations/{cid}/messages")).json()
    assert body["messages"][-1]["content"] == chat_mod.INTERRUPTED_MARKER
    r = await asyncio.wait_for(post, 5)
    assert chat_mod.INTERRUPTED_MARKER in r.text


def _fake_model(content="hello there, friend"):
    class FakeModel:
        async def complete(self, messages, tools=None, **kw):
            yield {"type": "token", "text": content}
            yield {"type": "message", "content": content,
                   "tool_calls": None, "usage": {}}
    return FakeModel()


async def _final_of(gen):
    final = None
    async for ev in gen:
        if ev["type"] == "final":
            final = ev
    return final


async def test_rewrite_rules_default_still_rewrites(tmp_env, monkeypatch):
    """Sanity: with standing rules present, the default path still runs the
    second-pass rewrite (behavior unchanged for non-voice turns)."""
    from backend.agent import loop as loop_mod

    async def fake_enforce(content, rules):
        return "REWRITTEN"

    monkeypatch.setattr(loop_mod, "model", _fake_model())
    monkeypatch.setattr(loop_mod, "standing_rules_tail", lambda: "no em-dashes")
    monkeypatch.setattr(loop_mod, "_enforce_rules", fake_enforce)

    final = await _final_of(loop_mod.run_turn(1, "sys", [
        {"role": "user", "content": "hi"}], tools=[]))
    assert final["content"] == "REWRITTEN"


async def test_rewrite_rules_off_skips_enforce(tmp_env, monkeypatch):
    """Voice turns (rewrite_rules=False) must never touch _enforce_rules —
    the streamed text was already spoken; rewriting it would silently
    diverge from what the operator heard."""
    from backend.agent import loop as loop_mod

    async def exploding_enforce(content, rules):
        raise AssertionError("_enforce_rules must not run for voice turns")

    monkeypatch.setattr(loop_mod, "model", _fake_model())
    monkeypatch.setattr(loop_mod, "standing_rules_tail", lambda: "no em-dashes")
    monkeypatch.setattr(loop_mod, "_enforce_rules", exploding_enforce)

    final = await _final_of(loop_mod.run_turn(1, "sys", [
        {"role": "user", "content": "hi"}], tools=[], rewrite_rules=False))
    assert final["content"] == "hello there, friend"
