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


async def first_tts(session, n=1):
    """Poll until the session has sent >= n TTS requests to the sidecar."""
    for _ in range(300):
        tts = [m for m in session.link.sent_json if m["type"] == "tts"]
        if len(tts) >= n:
            return tts
        await asyncio.sleep(0.01)
    raise AssertionError("no TTS request reached the sidecar")


async def test_happy_path_speaks_sentences_and_persists(seeded, monkeypatch):
    from backend import chat as chat_mod
    session, out = make_session(monkeypatch)
    monkeypatch.setattr(chat_mod, "guest_turn", scripted_turn(
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


# ---- barge-in ----------------------------------------------------------------

def blocking_prose_turn(release: asyncio.Event, first_sentence: str):
    """First call: streams one sentence then blocks (cancellable mid-turn).
    Later calls: answer immediately."""
    calls = {"n": 0}

    async def turn(cid, system_prompt, history, tools=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"type": "token", "text": first_sentence}
            await release.wait()
            yield {"type": "final", "content": first_sentence + " And more."}
        else:
            yield {"type": "token", "text": "Second answer, quick and done. "}
            yield {"type": "final", "content": "Second answer, quick and done. "}
    return turn


async def test_barge_in_real_speech_cancels_and_annotates(seeded, monkeypatch):
    from backend import chat as chat_mod, voice
    from backend.voice_text import CUTOFF_MARK
    session, out = make_session(monkeypatch)
    release = asyncio.Event()
    sentence = "Let me walk you through the whole history of this. "
    monkeypatch.setattr(chat_mod, "guest_turn",
                        blocking_prose_turn(release, sentence))

    await session._on_transcript("tell me everything")
    tts = await first_tts(session)            # sentence chunk reaches TTS
    assert session.state == "speaking"
    assert len(tts) == 1

    # played 100% of chunk 1 (dur known via tts_done), then the operator talks
    await session._on_sidecar_json(
        {"type": "tts_done", "id": tts[0]["id"], "dur_ms": 2000})
    await session.on_browser_json(
        {"type": "barge_in", "chunk_id": tts[0]["id"], "played_ms": 2000})
    assert session.state == "barge_pending"
    # turn is NOT cancelled while the verdict is pending
    assert not session.turn_task.done()

    await session._on_transcript("actually stop, different question")
    await settle(session)

    # sidecar told to cancel TTS, browser told to stop playback
    assert {"type": "tts_cancel"} in session.link.sent_json
    assert any(isinstance(m, dict) and m.get("type") == "stop_playback"
               for m in out)

    db = await get_db()
    try:
        async with db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "ORDER BY id", (session.cid,)) as cur:
            rows = [(r["role"], r["content"]) for r in await cur.fetchall()]
    finally:
        await db.close()
    # the interrupted reply carries the spoken prefix + the cut-off marker
    assert rows[1][0] == "assistant"
    assert rows[1][1].startswith(sentence.strip())
    assert CUTOFF_MARK in rows[1][1]
    # and the new utterance became the next exchange
    assert rows[2] == ("user", "actually stop, different question")
    assert rows[3][0] == "assistant" and "Second answer" in rows[3][1]


async def test_barge_in_false_alarm_resumes(seeded, monkeypatch):
    from backend import chat as chat_mod
    session, out = make_session(monkeypatch)
    release = asyncio.Event()
    sentence = "Here is a very long explanation you asked about earlier. "
    monkeypatch.setattr(chat_mod, "guest_turn",
                        blocking_prose_turn(release, sentence))

    await session._on_transcript("go on then")
    tts = await first_tts(session)
    await session.on_browser_json(
        {"type": "barge_in", "chunk_id": tts[0]["id"], "played_ms": 300})
    assert session.state == "barge_pending"

    await session._on_transcript("")          # cough — sidecar heard nothing
    assert session.state == "speaking"
    assert any(isinstance(m, dict) and m.get("type") == "resume_playback"
               for m in out)
    assert {"type": "tts_cancel"} not in session.link.sent_json
    assert not session.turn_task.done()       # the reply was never disturbed

    release.set()
    await settle(session)


async def test_barge_on_finished_turn_annotates_heard_upto(seeded, monkeypatch):
    from backend import chat as chat_mod
    session, out = make_session(monkeypatch)
    text = "The full answer is forty-two, per the usual sources. "
    monkeypatch.setattr(chat_mod, "guest_turn", scripted_turn(text))

    await session._on_transcript("what is the answer")
    await settle(session)
    assert session.state == "speaking"        # audio tail still playing
    tts = [m for m in session.link.sent_json if m["type"] == "tts"]
    await session._on_sidecar_json(
        {"type": "tts_done", "id": tts[0]["id"], "dur_ms": 1000})

    await session.on_browser_json(
        {"type": "barge_in", "chunk_id": tts[0]["id"], "played_ms": 500})
    await session._on_transcript("hang on, new thing")
    await settle(session)

    db = await get_db()
    try:
        async with db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "ORDER BY id", (session.cid,)) as cur:
            rows = [(r["role"], r["content"]) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert rows[1][0] == "assistant"
    assert "playback was interrupted" in rows[1][1]
    assert "…" in rows[1][1]
    assert rows[2] == ("user", "hang on, new thing")


# NOTE: speech during a tool-running turn is covered in test_voice_clone.py —
# under the worker cap it clones (test_talk_while_working_clones_and_delivers),
# at the cap it parks (test_cap_queues_instead_of_fourth_clone).
