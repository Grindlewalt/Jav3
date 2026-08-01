"""VoiceSession driven end-to-end with fake transports: queue-backed browser
sends, a scripted sidecar link, and a stubbed run_turn — no model, no
websockets, no audio. What's under test is the orchestration: transcript →
turn → sentence chunks → TTS requests → playback acks → idle."""
import asyncio

import pytest

from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.memory import ensure_memory_seeds


@pytest.fixture
async def seeded(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()


class FakeLink:
    """Stands in for SidecarLink: records what the session asked the voicebox
    to do; the test injects sidecar events by calling the session directly."""

    def __init__(self):
        self.sent_json = []
        self.connected = True
        self.ready = asyncio.Event()
        self.ready.set()

    def start(self):
        pass

    async def send_json(self, obj):
        self.sent_json.append(obj)
        return True

    async def send_bytes(self, data):
        return True

    async def stop(self):
        pass


def make_session(monkeypatch):
    from backend import voice
    monkeypatch.setattr(voice, "in_peak_window", lambda: False)
    to_browser = []

    async def send_json(obj):
        to_browser.append(obj)

    async def send_bytes(data):
        to_browser.append(data)

    s = voice.VoiceSession(send_json, send_bytes)
    s.link = FakeLink()
    return s, to_browser


def scripted_turn(*pieces, final=None):
    async def turn(cid, system_prompt, history, tools=None, **kw):
        for p in pieces:
            yield {"type": "token", "text": p}
        yield {"type": "final", "content": final if final is not None
               else "".join(pieces)}
    return turn


async def settle(session):
    """Wait out the detached turn + consumer tasks."""
    from backend import chat as chat_mod
    for t in list(chat_mod._active_turns.values()):
        await asyncio.wait([t], timeout=5)
    if session.turn_consumer:
        await asyncio.wait_for(session.turn_consumer, 5)


async def test_happy_path_speaks_sentences_and_persists(seeded, monkeypatch):
    from backend import chat as chat_mod
    session, out = make_session(monkeypatch)
    monkeypatch.setattr(chat_mod, "run_turn", scripted_turn(
        "The weather is looking pretty clear today. ",
        "Do you want the full forecast"))

    await session._on_transcript("what's the weather")
    await settle(session)

    # both sentences reached the sidecar as TTS requests, in order
    tts = [m for m in session.link.sent_json if m["type"] == "tts"]
    assert [t["text"] for t in tts] == [
        "The weather is looking pretty clear today.",
        "Do you want the full forecast"]          # tail flushed at final

    # browser saw the transcript, both chunks, and a speaking state
    kinds = [m["type"] for m in out if isinstance(m, dict)]
    assert "transcript" in kinds and kinds.count("assistant_text") == 2
    assert session.state == "speaking"

    # the reply persisted through the ordinary chat path
    db = await get_db()
    try:
        async with db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "ORDER BY id", (session.cid,)) as cur:
            rows = [(r["role"], r["content"]) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert rows[0] == ("user", "what's the weather")
    assert rows[1][0] == "assistant"
    assert "full forecast" in rows[1][1]

    # playback acks drain it to listening
    for t in tts:
        await session._on_sidecar_json(
            {"type": "tts_done", "id": t["id"], "dur_ms": 1000})
        await session.on_browser_json(
            {"type": "chunk_played", "chunk_id": t["id"]})
    assert session.state == "listening"


async def test_second_utterance_queues_behind_tool_turn(seeded, monkeypatch):
    """While the turn is mid-tool, new speech parks (phase-4 clones replace
    this) and a canned busy line is spoken."""
    from backend import chat as chat_mod, voice
    session, out = make_session(monkeypatch)
    release = asyncio.Event()

    async def tool_turn(cid, system_prompt, history, tools=None, **kw):
        yield {"type": "tool", "id": "t1", "name": "web_search", "args": {}}
        await release.wait()
        yield {"type": "tool_result", "id": "t1", "name": "web_search",
               "ok": True, "result": "…"}
        yield {"type": "final", "content": "Found it: the answer is 42."}

    monkeypatch.setattr(chat_mod, "run_turn", tool_turn)
    await session._on_transcript("look something up")
    for _ in range(50):                      # let the tool event land
        await asyncio.sleep(0.01)
        if session.turn_saw_tool:
            break
    assert session.turn_saw_tool

    await session._on_transcript("also what time is it")
    assert session.queued == ["also what time is it"]
    busy = [m for m in session.link.sent_json
            if m["type"] == "tts" and m["text"] == voice.BUSY_LINE]
    assert busy, "the canned busy line should be spoken"

    release.set()
    await settle(session)
    # the queued utterance drains once the turn's audio is acked
    tts = [m for m in session.link.sent_json if m["type"] == "tts"
           and m["text"] != voice.BUSY_LINE]
    for t in tts:
        await session._on_sidecar_json(
            {"type": "tts_done", "id": t["id"], "dur_ms": 500})
        await session.on_browser_json(
            {"type": "chunk_played", "chunk_id": t["id"]})
    await settle(session)
    db = await get_db()
    try:
        async with db.execute(
            "SELECT content FROM messages WHERE conversation_id = ? AND "
            "role = 'user' ORDER BY id", (session.cid,)) as cur:
            users = [r["content"] for r in await cur.fetchall()]
    finally:
        await db.close()
    assert users == ["look something up", "also what time is it"]
