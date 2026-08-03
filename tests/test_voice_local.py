"""The local fast tier: voice turns route to the operator's ollama, escalate
to DeepSeek only by [ESCALATE] + spoken permission (or the smart-model
keyword), and the peak gate never fires for local inference."""
import asyncio

import pytest

from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.memory import ensure_memory_seeds

from tests.test_voice_session import make_session, settle


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


def local_tier(monkeypatch, model="llama3.1:8b"):
    from backend import voice
    monkeypatch.setattr(voice.settings, "voice_local_model", model)


def capturing_turn(script):
    """script: list of (tokens, final) per call; records each call's model
    kwargs so routing is assertable."""
    calls = []

    async def turn(cid, system_prompt, history, tools=None, **kw):
        idx = len(calls)
        calls.append({"model_name": kw.get("model_name"),
                      "base_url": kw.get("base_url"),
                      "rewrite_rules": kw.get("rewrite_rules"),
                      "system_prompt": system_prompt})
        tokens, final = script[min(idx, len(script) - 1)]
        for t in tokens:
            yield {"type": "token", "text": t}
        yield {"type": "final", "content": final}
    return turn, calls


async def rows_of(cid):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "ORDER BY id", (cid,)) as cur:
            return [(r["role"], r["content"]) for r in await cur.fetchall()]
    finally:
        await db.close()


async def test_local_routing_and_prompt(seeded, monkeypatch):
    from backend import chat as chat_mod, voice
    from backend.voice_text import LOCAL_PROMPT
    local_tier(monkeypatch)
    session, out = make_session(monkeypatch)
    turn, calls = capturing_turn([
        (["It's about, three thirty in the afternoon. "],
         "It's about, three thirty in the afternoon. ")])
    monkeypatch.setattr(chat_mod, "guest_turn", turn)

    await session._on_transcript("what time is it")
    await settle(session)

    assert calls[0]["model_name"] == "llama3.1:8b"
    assert calls[0]["base_url"] == voice.settings.voice_local_base_url
    assert LOCAL_PROMPT in calls[0]["system_prompt"]
    # M4e: these three ride the guest loop, which is now the ONLY loop. Before
    # it, they were passed to a host path production never took — the local
    # tier was configured on the Pi and silently answering from DeepSeek.
    assert calls[0]["rewrite_rules"] is False   # already spoken; don't re-pass
    tts = [m for m in session.link.sent_json if m["type"] == "tts"]
    assert tts, "local reply must be spoken"


async def test_smart_word_skips_local(seeded, monkeypatch):
    from backend import chat as chat_mod
    from backend.voice_text import LOCAL_PROMPT
    local_tier(monkeypatch)
    session, out = make_session(monkeypatch)
    turn, calls = capturing_turn([([], "On it.")])
    monkeypatch.setattr(chat_mod, "guest_turn", turn)

    await session._on_transcript("use the smart model to plan my week")
    await settle(session)

    assert calls[0]["model_name"] is None       # straight to DeepSeek
    assert calls[0]["base_url"] is None
    assert LOCAL_PROMPT not in calls[0]["system_prompt"]


async def test_escalation_ask_and_accept(seeded, monkeypatch):
    from backend import chat as chat_mod, voice
    local_tier(monkeypatch)
    session, out = make_session(monkeypatch)
    turn, calls = capturing_turn([
        # call 1 (local): bounces — tokens split mid-marker to exercise the hold
        (["[ESCA", "LATE] That needs proper research. "],
         "[ESCALATE] That needs proper research."),
        # call 2 (smart rerun)
        (["Here's the full picture, researched properly. "],
         "Here's the full picture, researched properly. "),
    ])
    monkeypatch.setattr(chat_mod, "guest_turn", turn)

    await session._on_transcript("compare every music server out there")
    await settle(session)

    # the protocol line was never spoken; the ask was
    texts = [m["text"] for m in session.link.sent_json if m["type"] == "tts"]
    assert not any("ESCALATE" in t for t in texts)
    assert any(voice.ESCALATE_ASK in t for t in texts)
    assert session.state == "confirm_escalate"
    # the escalate row is scrubbed: history ends at the operator's utterance
    assert (await rows_of(session.cid))[-1] == \
        ("user", "compare every music server out there")

    await session._on_transcript("yes, send it")
    await settle(session)
    assert calls[1]["model_name"] is None       # rerun went to DeepSeek
    rows = await rows_of(session.cid)
    assert rows[-1][0] == "assistant" and "full picture" in rows[-1][1]
    # exactly one user row — the rerun didn't re-insert
    assert [r for r in rows if r[0] == "user"] == \
        [("user", "compare every music server out there")]


async def test_escalation_declined(seeded, monkeypatch):
    from backend import chat as chat_mod, voice
    local_tier(monkeypatch)
    session, out = make_session(monkeypatch)
    turn, calls = capturing_turn([([], "[ESCALATE] Too big for me.")])
    monkeypatch.setattr(chat_mod, "guest_turn", turn)

    await session._on_transcript("audit the whole codebase")
    await settle(session)
    assert session.state == "confirm_escalate"

    await session._on_transcript("no, don't bother")
    assert session.state == "listening"
    assert len(calls) == 1                      # no smart rerun
    texts = [m["text"] for m in session.link.sent_json if m["type"] == "tts"]
    assert voice.ESCALATE_DROPPED in texts


async def test_local_turns_skip_peak_gate(seeded, monkeypatch):
    from backend import chat as chat_mod, voice
    local_tier(monkeypatch)
    session, out = make_session(monkeypatch)   # in_peak_window -> False here
    monkeypatch.setattr(voice, "in_peak_window", lambda: True)  # force peak ON
    turn, calls = capturing_turn([([], "Cheap and local, any hour.")])
    monkeypatch.setattr(chat_mod, "guest_turn", turn)

    await session._on_transcript("quick one, what's two plus two")
    await settle(session)
    # no CONFIRM_PEAK detour — the local turn just ran
    assert calls and calls[0]["model_name"] == "llama3.1:8b"
    assert session.pending_peak is None


async def test_confirm_state_survives_ask_playback(seeded, monkeypatch):
    """Acking the spoken ask's audio must not flip a confirm state back to
    listening (the pre-existing _maybe_idle stomp)."""
    from backend import chat as chat_mod
    local_tier(monkeypatch)
    session, out = make_session(monkeypatch)
    turn, calls = capturing_turn([([], "[ESCALATE] Needs the big model.")])
    monkeypatch.setattr(chat_mod, "guest_turn", turn)

    await session._on_transcript("do the enormous thing")
    await settle(session)
    assert session.state == "confirm_escalate"
    ask = [m for m in session.link.sent_json if m["type"] == "tts"][-1]
    await session._on_sidecar_json(
        {"type": "tts_done", "id": ask["id"], "dur_ms": 900})
    await session.on_browser_json(
        {"type": "chunk_played", "chunk_id": ask["id"]})
    assert session.state == "confirm_escalate"  # still waiting for yes/no
