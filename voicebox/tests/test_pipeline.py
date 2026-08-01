"""Round-trip the voicebox pipeline: synthesize a sentence with Kokoro, feed
it back through silero VAD + faster-whisper, and expect the words back.

Self-skips when the model files aren't present, so this only runs on a dev
box / the sidecar host after `download_models.py` — never in the main repo's
suite (root pytest.ini pins testpaths=tests).

Run:  cd voicebox && .venv/bin/python -m pytest tests/ -q
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tts import MODEL_FILE, VOICES_FILE, models_dir  # noqa: E402

MODELS = models_dir()
pytestmark = pytest.mark.skipif(
    not (MODELS / MODEL_FILE).exists() or not (MODELS / VOICES_FILE).exists(),
    reason="voicebox models not downloaded (run download_models.py)")


def _resample_24k_to_16k(pcm: bytes) -> bytes:
    """Linear-interp resample, PCM16 mono. Test-only — production never
    resamples (the browser captures at 16 kHz, kokoro plays back at 24 kHz)."""
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n_out = int(len(x) * 16_000 / 24_000)
    idx = np.linspace(0, len(x) - 1, n_out)
    y = np.interp(idx, np.arange(len(x)), x)
    return y.astype(np.int16).tobytes()


@pytest.fixture(scope="module")
def synth():
    from tts import Synth
    return Synth()


@pytest.fixture(scope="module")
def transcriber():
    from stt import Transcriber
    return Transcriber(download_root=str(MODELS / "whisper"))


def test_tts_produces_audio(synth):
    pcm = synth.synth("The quick brown fox jumps over the lazy dog.")
    assert len(pcm) > 24_000            # > 0.5 s of 24 kHz PCM16
    assert len(pcm) % 2 == 0


def test_vad_ignores_silence():
    from vad import StreamingVAD
    vad = StreamingVAD()
    events = list(vad.feed(b"\x00" * 16_000 * 2 * 2))   # 2 s of silence
    assert events == []


def test_roundtrip_tts_vad_stt(synth, transcriber):
    from vad import StreamingVAD
    pcm16k = _resample_24k_to_16k(
        synth.synth("The quick brown fox jumps over the lazy dog."))
    vad = StreamingVAD()
    utterances = []
    for ev, payload in vad.feed(pcm16k + b"\x00" * 16_000):  # trailing silence
        if ev == "speech_end":
            utterances.append(payload)
    assert utterances, "VAD never detected the synthesized speech"
    text = transcriber.transcribe(max(utterances, key=len)).lower()
    assert "quick brown fox" in text
