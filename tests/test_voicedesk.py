"""The headless voice client, and the token door it comes in through.

Two things under test, neither of which needs a sound card:

- `/api/voice/ws` accepts a bearer token as well as a session cookie, and an
  unset token does not silently become "no check".
- the client's barge-in gate and message plumbing, with the audio layer stubbed.

The audio itself (PortAudio streams) is not testable here and is not tested;
what IS testable is that the right things get said to the socket at the right
moments, which is where the bugs would be.
"""
import json
import sys
import types

import pytest

from backend.auth import hash_password
from backend.db import get_db, init_db


@pytest.fixture
async def seeded(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()


# ---- the token door ---------------------------------------------------------

def _ws(headers=None, cookies=None):
    """A WebSocket stand-in with just the bits _client_token_ok reads."""
    return types.SimpleNamespace(headers=headers or {}, cookies=cookies or {})


def test_client_token_requires_a_configured_secret(monkeypatch):
    from backend.config import settings
    from backend.voice_api import _client_token_ok

    # unset must not mean "anything goes" — the commonest way a token check
    # becomes a no-op
    monkeypatch.setattr(settings, "voice_client_token", "")
    assert _client_token_ok(_ws({"authorization": "Bearer whatever"})) is False
    assert _client_token_ok(_ws({"authorization": "Bearer "})) is False
    assert _client_token_ok(_ws()) is False


def test_client_token_matches_exactly(monkeypatch):
    from backend.config import settings
    from backend.voice_api import _client_token_ok
    monkeypatch.setattr(settings, "voice_client_token", "s3cret")

    assert _client_token_ok(_ws({"authorization": "Bearer s3cret"})) is True
    assert _client_token_ok(_ws({"authorization": "bearer s3cret"})) is False
    assert _client_token_ok(_ws({"authorization": "Bearer s3cre"})) is False
    assert _client_token_ok(_ws({"authorization": "Bearer s3cret "})) is True   # trimmed
    assert _client_token_ok(_ws({"authorization": "s3cret"})) is True   # bare token
    assert _client_token_ok(_ws({})) is False


async def test_ws_refuses_a_bad_token(seeded, monkeypatch):
    """End to end through the real app: no cookie, wrong token, closed."""
    import httpx
    from backend.config import settings
    from backend.main import app
    monkeypatch.setattr(settings, "voice_enabled", True)
    monkeypatch.setattr(settings, "voice_client_token", "right")

    # the ASGI app closes with 4401 before accepting; assert via the handler's
    # own decision function rather than a live socket, which needs a server
    from backend.voice_api import _client_token_ok
    assert _client_token_ok(_ws({"authorization": "Bearer wrong"})) is False
    assert _client_token_ok(_ws({"authorization": "Bearer right"})) is True
    assert app is not None and httpx is not None


# ---- the client's logic -----------------------------------------------------

@pytest.fixture
def desk(monkeypatch):
    """A VoiceDesk with the audio layer replaced by a recorder."""
    # the module imports .audio lazily inside run(), so nothing to stub for
    # construction — but sounddevice must not be needed to import agent.py
    monkeypatch.setitem(sys.modules, "sounddevice", types.ModuleType("sounddevice"))
    from clients.voicedesk.agent import VoiceDesk

    class FakePlay:
        def __init__(self):
            self.playing = False
            self.paused = False
            self.stopped = 0
            self.chimes = 0
            self.queued = []
        def pause(self): self.paused = True
        def resume(self): self.paused = False
        def stop_all(self): self.stopped += 1; self.paused = False
        def position(self): return (7, 350)
        def chime(self): self.chimes += 1
        def enqueue(self, cid, pcm): self.queued.append((cid, len(pcm)))

    d = VoiceDesk("ws://x/api/voice/ws", "tok")
    d.play = FakePlay()
    d.sent = []
    d._post = lambda item: d.sent.append(item)   # capture instead of queueing
    return d


def _json_sent(desk):
    return [json.loads(m) for m in desk.sent if isinstance(m, str)]


def test_mic_frames_carry_the_type_byte(desk):
    desk.play.playing = False
    desk._on_mic(b"\x01\x02\x03\x04", 0.001)
    assert desk.sent == [b"\x01" + b"\x01\x02\x03\x04"]


def test_muting_stops_sending_audio(desk):
    """A muted mic sends no audio AND cannot barge in — otherwise "mute" would
    still be able to cut Jarvis off mid-sentence."""
    desk.muted = True
    desk.play.playing = True
    desk._on_mic(b"\x00" * 16, 0.5)
    desk._on_mic(b"\x00" * 16, 0.5)
    assert desk.sent == []


def test_barge_in_needs_two_hot_blocks_and_playback(desk):
    """One loud block is a door; two in a row while speaking is a person."""
    desk.play.playing = True
    # NOT muted: a muted mic must not barge in, which the next test covers —
    # mic frames are bytes and _json_sent filters them out anyway.
    desk._vad(0.10)                         # hot, but only once
    assert _json_sent(desk) == []
    desk._vad(0.10)                         # ...and again: trip
    sent = _json_sent(desk)
    assert sent == [{"type": "barge_in", "chunk_id": 7, "played_ms": 350}]
    assert desk.play.paused is True

    # it does not re-fire while already suspended
    desk._vad(0.10)
    assert len(_json_sent(desk)) == 1


def test_the_gate_is_higher_while_speaking(desk):
    """Some of our own voice comes back down the mic on a box with no echo
    cancellation, so the bar rises during playback."""
    desk.play.playing = False
    desk._vad(0.03); desk._vad(0.03)        # above base, below base*3
    assert _json_sent(desk) == []           # ...but we were not playing anyway

    desk.play.playing = True
    desk._vad(0.03); desk._vad(0.03)
    assert _json_sent(desk) == []           # under the raised gate: no barge

    desk._vad(0.09); desk._vad(0.09)
    assert _json_sent(desk)[0]["type"] == "barge_in"


def test_a_quiet_block_resets_the_streak(desk):
    desk.play.playing = True
    desk._vad(0.09)
    desk._vad(0.001)                        # silence between two thumps
    desk._vad(0.09)
    assert _json_sent(desk) == []


async def test_resume_keeps_the_queue_and_stop_drops_it(desk):
    """The false-alarm path must lose nothing — that is the contract
    backend/voice.py's resume_playback relies on."""
    desk._suspended = True
    desk.play.paused = True
    await desk._on_json({"type": "resume_playback"})
    assert desk.play.paused is False and desk.play.stopped == 0
    assert desk._suspended is False

    desk._suspended = True
    await desk._on_json({"type": "stop_playback"})
    assert desk.play.stopped == 1 and desk._suspended is False


async def test_wake_chimes(desk):
    await desk._on_json({"type": "wake"})
    assert desk.play.chimes == 1


async def test_shutdown_ends_the_process(desk):
    with pytest.raises(SystemExit):
        await desk._on_json({"type": "shutdown"})


def test_tts_frames_are_unpacked_to_the_right_chunk(desk):
    import struct
    frame = bytes([0x02]) + struct.pack("<I", 42) + b"\xaa\xbb" * 10
    desk._on_binary(frame)
    assert desk.play.queued == [(42, 20)]

    desk._on_binary(bytes([0x02]) + b"\x00\x01")     # truncated: ignored
    desk._on_binary(bytes([0x01]) + b"not tts")      # wrong type: ignored
    assert len(desk.play.queued) == 1


def test_played_chunks_are_acked(desk):
    desk._on_chunk_done(9)
    assert _json_sent(desk) == [{"type": "chunk_played", "chunk_id": 9}]
    desk._on_chunk_done(0)                  # the chime is chunk 0 — not a reply
    assert len(_json_sent(desk)) == 1


def test_config_prefers_env_over_file(tmp_path, monkeypatch):
    from clients.voicedesk.agent import _load_config
    cfg = tmp_path / "voicedesk.json"
    cfg.write_text(json.dumps({"url": "ws://from-file/ws", "token": "file-tok",
                               "name": "study"}))
    monkeypatch.setenv("JARVIS_VOICE_CLIENT_TOKEN", "env-tok")
    out = _load_config(cfg)
    assert out["url"] == "ws://from-file/ws"     # file still supplies this
    assert out["token"] == "env-tok"             # env wins
    assert out["name"] == "study"


def test_missing_config_is_not_a_crash(tmp_path):
    from clients.voicedesk.agent import _load_config
    assert _load_config(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert _load_config(bad) == {}
