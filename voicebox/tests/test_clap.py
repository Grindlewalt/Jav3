"""Clap detector: does it fire on claps and stay quiet on everything else.

The point of moving this out of the browser was testability — the old
peak-threshold version could only be evaluated by clapping at a laptop. These
build the confusable sounds synthetically (a clap is a broadband burst with a
near-vertical attack; a thud is the same envelope lowpassed; a vowel is
harmonic and sustained) and assert the discriminator, not the threshold.
"""
import numpy as np
import pytest

from clap import (GAP_MAX_MS, GAP_MIN_MS, RATE, ClapDetector, clap_features)

rng = np.random.default_rng(7)


def pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def silence(ms: int, level: float = 0.001) -> np.ndarray:
    return (rng.standard_normal(int(RATE * ms / 1000)) * level).astype(np.float32)


def clap(ms: int = 40, amp: float = 0.8) -> np.ndarray:
    """Broadband burst, ~1 ms attack, exponential decay."""
    n = int(RATE * ms / 1000)
    env = np.exp(-np.linspace(0, 9, n))
    attack = int(RATE * 0.001)
    env[:attack] *= np.linspace(0, 1, attack)
    return (rng.standard_normal(n) * env * amp).astype(np.float32)


def thud(ms: int = 60, amp: float = 0.8) -> np.ndarray:
    """Same envelope, but low-frequency only — a door or a fist on a desk."""
    n = int(RATE * ms / 1000)
    t = np.arange(n) / RATE
    env = np.exp(-np.linspace(0, 8, n))
    wave = np.sin(2 * np.pi * 90 * t) + 0.5 * np.sin(2 * np.pi * 160 * t)
    return (wave * env * amp).astype(np.float32)


def vowel(ms: int = 400, amp: float = 0.5) -> np.ndarray:
    """Sustained harmonic stack — a voice."""
    n = int(RATE * ms / 1000)
    t = np.arange(n) / RATE
    w = sum(np.sin(2 * np.pi * f * t) / k
            for k, f in enumerate((130, 260, 520, 780), start=1))
    env = np.minimum(1.0, np.linspace(0, 6, n))
    return (w * env * amp / 2).astype(np.float32)


def run(det: ClapDetector, chunks: list[np.ndarray]) -> int:
    """Feed in 20 ms frames the way the sidecar does; count fires."""
    audio = np.concatenate(chunks)
    fires = 0
    step = int(RATE * 0.02)
    for i in range(0, len(audio) - step + 1, step):
        if det.feed(pcm(audio[i:i + step])):
            fires += 1
    return fires


def test_double_clap_fires_once():
    det = ClapDetector()
    assert run(det, [silence(600), clap(), silence(250),
                     clap(), silence(600)]) == 1


def test_single_clap_does_not_fire():
    det = ClapDetector()
    assert run(det, [silence(600), clap(), silence(1500)]) == 0


def test_gap_outside_the_window_does_not_pair():
    for gap, label in ((int(GAP_MIN_MS) - 60, "too fast"),
                       (int(GAP_MAX_MS) + 400, "too slow")):
        det = ClapDetector()
        assert run(det, [silence(600), clap(), silence(gap),
                         clap(), silence(600)]) == 0, label


def test_thuds_do_not_fire():
    """The failure the operator actually hit: loud, sharp, not a clap."""
    det = ClapDetector()
    assert run(det, [silence(600), thud(), silence(250),
                     thud(), silence(600)]) == 0


def test_speech_does_not_fire():
    det = ClapDetector()
    assert run(det, [silence(400), vowel(), silence(200),
                     vowel(300), silence(400)]) == 0


def test_quiet_claps_still_fire_because_the_floor_adapts():
    """A fixed threshold is what made this mic-gain dependent. At 1/8 the
    amplitude the SHAPE is identical, so it must still register."""
    det = ClapDetector()
    assert run(det, [silence(600, 0.0002), clap(amp=0.1), silence(250, 0.0002),
                     clap(amp=0.1), silence(600, 0.0002)]) == 1


def test_features_separate_clap_from_thud():
    c, t = clap_features(pcm(clap())), clap_features(pcm(thud()))
    assert c["hf_ok"] and c["duration_ok"]
    assert not t["hf_ok"]                     # the discriminator, stated plainly
    assert c["hf_ratio"] > t["hf_ratio"] * 3


def test_real_speech_recording_is_silent(tmp_path):
    """If a real utterance is available, it must not produce a single fire."""
    import wave
    from pathlib import Path
    wav = Path(__file__).with_name("speech16k.wav")
    if not wav.exists():
        pytest.skip("no speech16k.wav fixture next to the tests")
    with wave.open(str(wav)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1
        raw = w.readframes(w.getnframes())
    det = ClapDetector()
    fires = sum(det.feed(raw[i:i + 640]) for i in range(0, len(raw), 640))
    assert fires == 0
