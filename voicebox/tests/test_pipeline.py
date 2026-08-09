"""voicebox's own tests. Two tiers:

- **Pure** (always run): the hallucination filter and the VAD's evidence
  arithmetic. No models, no CUDA, no network — these are what the Pi's
  barge-in rules are written against, so they must be checkable anywhere.
- **Live** (self-skipping): the real round trip through architect-tts, silero
  and whisper. Needs the TTS service up and the whisper model present, so it
  only runs on the sidecar host / a dev box.

Never collected by the main repo's suite (root pytest.ini pins
testpaths=tests). Run:  cd voicebox && .venv/bin/python -m pytest tests/ -q
"""
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---- pure: the hallucination filter -----------------------------------------

@pytest.mark.parametrize("text", [
    "", "   ",
    "Thank you.", "thanks for watching!", "Please subscribe",
    "Bye.", "you", "Okay.", "Hmm.",
    "[Music]", "(applause)", "♪♪♪", "*music*",
    "Subtitles by the Amara.org community",
    "you you you you", "Thank you. Thank you. Thank you. Thank you.",
])
def test_is_phantom_catches_whisper_fabrications(text):
    """These come back fluent and correctly punctuated, so no confidence
    threshold catches them — they are named explicitly."""
    from stt import is_phantom
    assert is_phantom(text) is True


@pytest.mark.parametrize("text", [
    "Play Kickstart My Heart.",
    "Jarvis, turn it down.",
    "Stop.",                       # a legitimate one-word barge-in
    "No, cancel that.",
    "What did the overnight schedules turn up?",
    "Thank you for setting that up, it worked.",   # contains a phantom phrase
])
def test_is_phantom_keeps_real_speech(text):
    from stt import is_phantom
    assert is_phantom(text) is False


def test_result_confidence_gate():
    from stt import Result
    good = Result(text="turn the music down", no_speech_prob=0.05,
                  avg_logprob=-0.3, phantom=False)
    assert good.confident is True
    assert good.as_event()["confident"] is True

    assert Result(text="", phantom=True).confident is False
    assert Result(text="hi", no_speech_prob=0.9, avg_logprob=-0.2,
                  phantom=False).confident is False      # probably not speech
    assert Result(text="hi", no_speech_prob=0.1, avg_logprob=-3.0,
                  phantom=False).confident is False      # it was guessing
    assert Result(text="Thank you.", no_speech_prob=0.1, avg_logprob=-0.2,
                  phantom=True).confident is False       # fabrication


# ---- pure: the VAD's evidence ------------------------------------------------

def test_utterance_statistics():
    """speech_ratio is what separates the operator from a guitar; the numbers
    it produces have to be the ones backend/voice.py was tuned against."""
    from vad import KEEP_PROB, Utterance

    speech = Utterance(pcm=b"\0" * 32, probs=[0.9, 0.8, 0.95, 0.7, 0.85])
    assert speech.speech_ratio == 1.0
    assert 0.8 < speech.mean_prob < 0.9

    guitar = Utterance(pcm=b"\0" * 32, probs=[0.02, 0.10, 0.01, 0.15, 0.03])
    assert guitar.speech_ratio == 0.0
    assert guitar.mean_prob < KEEP_PROB

    empty = Utterance()
    assert empty.speech_ratio == 0.0 and empty.mean_prob == 0.0
    assert empty.as_event()["frames"] == 0

    # len() is the audio, not the frame count — callers sort utterances by size
    assert len(speech) == 32


def test_split_tts_text():
    from tts import PIECE_CHARS, split_tts_text
    assert split_tts_text("short line") == ["short line"]
    assert split_tts_text("") == []
    long = ("The first clause runs on for a while, then a second clause "
            "follows it, and finally a third one wraps the whole thing up "
            "after the pause. Then another sentence entirely.")
    pieces = split_tts_text(long)
    assert len(pieces) >= 2
    assert all(len(p) <= PIECE_CHARS + 2 for p in pieces)
    assert " ".join(pieces) == " ".join(long.split())   # nothing lost


# ---- live: the real round trip ----------------------------------------------

def _tts_up() -> bool:
    url = os.environ.get("VOICEBOX_TTS_URL", "http://127.0.0.1:8123")
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


live = pytest.mark.skipif(not _tts_up(),
                          reason="architect-tts is not running (see "
                                 "voicebox/architect_tts.py)")


@pytest.fixture(scope="module")
def synth():
    from tts import Synth
    return Synth()


@pytest.fixture(scope="module")
def transcriber():
    from stt import Transcriber
    from tts import models_dir
    return Transcriber(download_root=str(models_dir() / "whisper"))


def _resample_24k_to_16k(pcm: bytes) -> bytes:
    """Test-only — production never resamples (mic captures at 16 kHz, the
    voice plays back at 24 kHz)."""
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n_out = int(len(x) * 16_000 / 24_000)
    y = np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x)
    return y.astype(np.int16).tobytes()


@live
def test_tts_produces_audio(synth):
    pcm = synth.synth("The quick brown fox jumps over the lazy dog.")
    assert len(pcm) > 24_000            # > 0.5 s of 24 kHz PCM16
    assert len(pcm) % 2 == 0


def test_vad_ignores_silence():
    from vad import StreamingVAD
    vad = StreamingVAD()
    assert list(vad.feed(b"\x00" * 16_000 * 2 * 2)) == []   # 2 s of silence


@live
def test_roundtrip_tts_vad_stt(synth, transcriber):
    from vad import StreamingVAD
    pcm16k = _resample_24k_to_16k(
        synth.synth("The quick brown fox jumps over the lazy dog."))
    vad = StreamingVAD()
    utterances = [payload for ev, payload
                  in vad.feed(pcm16k + b"\x00" * 16_000)     # trailing silence
                  if ev == "speech_end"]
    assert utterances, "VAD never detected the synthesized speech"

    utt = max(utterances, key=len)
    # real speech must clear the bar the Pi's barge-in gate uses
    assert utt.speech_ratio >= 0.30, f"speech_ratio {utt.speech_ratio}"

    res = transcriber.transcribe(utt.pcm)
    assert "quick brown fox" in res.text.lower()
    assert res.confident is True
    assert res.phantom is False


@live
def test_vocabulary_biases_the_decoder(transcriber):
    """set_vocab must accept, dedupe and cap — the Pi pushes the whole music
    library and an over-long bias list starts costing ordinary words."""
    n = transcriber.set_vocab(["Jarvis", "jarvis", "Kickstart My Heart", "",
                               "  Mockingbird  "])
    assert n == 3                        # case-insensitive dedupe, blanks gone
    assert transcriber.set_vocab([f"term number {i}" for i in range(500)]) < 500


@live
def test_silence_transcribes_to_nothing(transcriber):
    res = transcriber.transcribe(b"\x00" * 16_000 * 2)      # 1 s of digital silence
    assert res.confident is False
    assert res.phantom is True
