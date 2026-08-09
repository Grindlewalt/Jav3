"""The room, not the operator: barge-in evidence and the clap curfew.

The failure these cover is specific and was reported live — someone playing
guitar in the same room stopped Jarvis mid-sentence. The chain was: the
browser's RMS gate trips on any loud sound, whisper is handed music and
returns fluent invented words, and `if not text` read non-empty text as the
operator talking. The sidecar now reports evidence (silero's speech ratio,
whisper's own confidence, a known-fabrication check) and the session weighs it.

Same no-model harness as test_voice_session."""
import asyncio
from datetime import datetime

import pytest

from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.memory import ensure_memory_seeds

from tests.test_voice_session import (first_tts, make_session, scripted_turn,
                                      settle)


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


# Evidence shapes, as the sidecar sends them. The numbers are the measured
# medians from voicebox (see backend/voice.py's docstring), not invented.
SPEECH = {"phantom": False, "confident": True, "speech_ratio": 0.80,
          "mean_prob": 0.77, "no_speech_prob": 0.05, "avg_logprob": -0.30}
GUITAR = {"phantom": False, "confident": True, "speech_ratio": 0.05,
          "mean_prob": 0.09, "no_speech_prob": 0.40, "avg_logprob": -0.80}
UNSURE = {"phantom": False, "confident": False, "speech_ratio": 0.55,
          "mean_prob": 0.50, "no_speech_prob": 0.55, "avg_logprob": -1.60}
PHANTOM = {"phantom": True, "confident": False, "speech_ratio": 0.02,
           "mean_prob": 0.03, "no_speech_prob": 0.70, "avg_logprob": -1.10}


# ---- the interrupt gate ------------------------------------------------------

def test_interrupt_gate_weighs_evidence():
    from backend.voice import BARGE_MIN_SPEECH_RATIO, VoiceSession
    gate = VoiceSession._is_interrupt

    assert gate(SPEECH) is True
    assert gate(GUITAR) is False          # music: silero says it isn't speech
    assert gate(UNSURE) is False          # whisper was guessing
    assert gate(PHANTOM) is False         # "Thanks for watching"
    # no evidence at all = the caller vouched for it (the test seam)
    assert gate(None) is True

    # the operator talking OVER a guitar still gets through: the worst measured
    # speech_ratio in that condition was 0.39, the best music-only clip 0.15
    assert gate({**SPEECH, "speech_ratio": 0.39}) is True
    assert gate({**GUITAR, "speech_ratio": 0.15}) is False
    assert 0.15 < BARGE_MIN_SPEECH_RATIO <= 0.39


async def test_guitar_does_not_interrupt_a_reply(seeded, monkeypatch):
    """A loud room noise pauses playback locally, then resumes: the running
    turn survives and nothing is persisted as an interruption."""
    from backend import chat as chat_mod
    session, out = make_session(monkeypatch)
    monkeypatch.setattr(chat_mod, "guest_turn", scripted_turn(
        "The forecast is clear all week. ", "Highs around twenty two."))

    await session._on_transcript("what's the weather")
    tts = await first_tts(session, 1)
    session.state = "speaking"

    await session.on_browser_json(
        {"type": "barge_in", "chunk_id": tts[0]["id"], "played_ms": 300})
    assert session.state == "barge_pending"

    # whisper heard the guitar as words, but the evidence says otherwise
    await session._on_transcript("Should I stay all right now", GUITAR)

    kinds = [m["type"] for m in out if isinstance(m, dict)]
    assert "resume_playback" in kinds
    assert "stop_playback" not in kinds
    assert session.state == "speaking"
    assert not any(m["type"] == "tts_cancel" for m in session.link.sent_json)
    await settle(session)


async def test_real_speech_still_interrupts(seeded, monkeypatch):
    """The guard must not have broken barge-in itself."""
    from backend import chat as chat_mod
    session, out = make_session(monkeypatch)
    monkeypatch.setattr(chat_mod, "guest_turn", scripted_turn(
        "The forecast is clear all week. ", "Highs around twenty two."))

    await session._on_transcript("what's the weather")
    tts = await first_tts(session, 1)
    session.state = "speaking"

    await session.on_browser_json(
        {"type": "barge_in", "chunk_id": tts[0]["id"], "played_ms": 300})
    await session._on_transcript("actually, stop", SPEECH)

    kinds = [m["type"] for m in out if isinstance(m, dict)]
    assert "stop_playback" in kinds
    assert "resume_playback" not in kinds
    assert any(m["type"] == "tts_cancel" for m in session.link.sent_json)
    await settle(session)


async def test_phantom_transcript_starts_no_turn(seeded, monkeypatch):
    """Room tone transcribed as "Thank you for watching" must not become a
    conversation turn — this is the one that wakes him up at 2am."""
    session, out = make_session(monkeypatch)
    session.state = "listening"

    await session._on_transcript("Thank you for watching.", PHANTOM)

    assert session.turn_task is None
    assert session.cid is None
    assert not any(m.get("type") == "transcript" for m in out
                   if isinstance(m, dict))


# ---- the clap curfew ---------------------------------------------------------

@pytest.mark.parametrize("hhmm,muted", [
    ("22:29", False),      # one minute before
    ("22:30", True),       # the boundary is inclusive at the start
    ("23:59", True),
    ("00:00", True),       # the window wraps midnight
    ("03:00", True),
    ("07:29", True),
    ("07:30", False),      # ...and exclusive at the end
    ("12:00", False),
])
def test_clap_curfew_window(hhmm, muted):
    from backend.voice import in_clap_curfew
    hh, mm = (int(x) for x in hhmm.split(":"))
    assert in_clap_curfew(datetime(2026, 8, 9, hh, mm)) is muted


def test_clap_curfew_can_be_disabled(monkeypatch):
    from backend.config import settings
    from backend.voice import in_clap_curfew
    monkeypatch.setattr(settings, "voice_clap_curfew", False)
    assert in_clap_curfew(datetime(2026, 8, 9, 3, 0)) is False


def test_clap_curfew_ignores_a_degenerate_window(monkeypatch):
    from backend.config import settings
    from backend.voice import in_clap_curfew
    monkeypatch.setattr(settings, "voice_clap_curfew_start", "07:30")
    monkeypatch.setattr(settings, "voice_clap_curfew_end", "07:30")
    assert in_clap_curfew(datetime(2026, 8, 9, 7, 30)) is False


def test_clap_curfew_survives_a_garbled_setting(monkeypatch):
    """A bad env value must fall back to the documented window, not crash the
    transcript router."""
    from backend.config import settings
    from backend.voice import in_clap_curfew
    monkeypatch.setattr(settings, "voice_clap_curfew_start", "not a time")
    assert in_clap_curfew(datetime(2026, 8, 9, 3, 0)) is True


async def test_clap_muted_overnight(seeded, monkeypatch):
    from backend import voice as voice_mod
    session, out = make_session(monkeypatch)
    fired = []
    monkeypatch.setattr(voice_mod, "in_clap_curfew", lambda: True)
    monkeypatch.setattr(session, "_clap_play",
                        lambda: fired.append(True))     # never awaited

    await session._on_sidecar_json({"type": "clap"})
    await asyncio.sleep(0)

    assert fired == []
    assert session.clap_done is False       # the gesture is still unspent
    clap = [m for m in out if isinstance(m, dict) and m["type"] == "clap"]
    assert clap and clap[0]["curfew"] is True


async def test_clap_fires_during_the_day(seeded, monkeypatch):
    from backend import voice as voice_mod
    session, _out = make_session(monkeypatch)
    fired = asyncio.Event()
    monkeypatch.setattr(voice_mod, "in_clap_curfew", lambda: False)

    async def play():
        fired.set()
    monkeypatch.setattr(session, "_clap_play", play)

    await session._on_sidecar_json({"type": "clap"})
    await asyncio.wait_for(fired.wait(), 2)
    assert session.clap_done is True


# ---- the wake word is deliberately NOT curfewed -------------------------------

async def test_wake_word_works_during_the_curfew(seeded, monkeypatch):
    """Quiet hours mute the clap, not Jarvis. "hey Jarvis" takes a spoken
    sentence to fire, so it cannot go off by accident."""
    from backend import voice as voice_mod
    session, _out = make_session(monkeypatch)
    monkeypatch.setattr(voice_mod, "in_clap_curfew", lambda: True)
    session.wake_enabled = True
    session.state = voice_mod.ASLEEP

    await session._on_sidecar_json({"type": "wake"})
    assert session.state == voice_mod.LISTENING
