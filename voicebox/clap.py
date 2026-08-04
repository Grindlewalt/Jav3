"""Double-clap detection on the mic stream.

Replaces the browser's peak-threshold detector, which fired on keyboard slams,
mugs on desks and door closes. Amplitude alone cannot tell those apart from a
clap — they are all loud. What separates a clap is its SHAPE:

  1. it rises out of nothing in about a millisecond (near-vertical attack),
  2. the whole burst is over in well under a tenth of a second,
  3. it is broadband — two slabs of skin slapping air put energy everywhere,
     where a door thud is bass and a vowel is a handful of low harmonics,
  4. it decays smoothly instead of sustaining.

So this measures all four, against a noise floor that TRACKS THE ROOM instead
of a fixed constant. That last part is what actually fixes over-sensitivity:
`peak > 0.4` means something different on a quiet desk mic and a hot gain
stage, and the operator's complaint is a symptom of exactly that.

Sidecar-side, not browser-side, for three reasons: the raw 16 kHz PCM is here
(the browser only sees 60 ms RMS batches — a clap is 10 ms, so it never had
the resolution to measure a duration), numpy is here, and being here it can be
tested offline against recorded audio, which the browser version never could.

    det = ClapDetector()
    if det.feed(pcm16_bytes):    # True = a double clap just completed
        ...
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np

RATE = 16_000
HOP = 160                       # 10 ms — fine enough to time a 30 ms burst
_WIN = 512                      # FFT window on the onset frame

# --- what a clap looks like ---------------------------------------------------
ONSET_RATIO = 8.0        # burst energy over the tracked floor to open a candidate
ATTACK_RATIO = 4.0       # ...and over the hop before it: this is the "sharp" test
MIN_BURST_MS = 10        # shorter than this is a click/pop, not hands
MAX_BURST_MS = 130       # longer than this is a voice, a drawer, music
HF_MIN = 0.35            # fraction of energy above 1.8 kHz (broadband test)
FLOOR_MIN = 1e-4         # never divide by a digital-silence floor

# --- what a DOUBLE clap looks like --------------------------------------------
GAP_MIN_MS = 120         # faster than this is one clap echoing off a wall
GAP_MAX_MS = 700         # slower than this is two unrelated noises
REFRACTORY_MS = 2000     # one fire per gesture

_FLOOR_ALPHA = 0.995     # slow tracker: the floor should follow the ROOM,
_FLOOR_ALPHA_UP = 0.90   # rising faster than it falls so a burst can't sink it


class ClapDetector:
    """Feed it mono PCM16 @ 16 kHz in any chunking. Returns True on the hop
    that completes a double clap."""

    def __init__(self, *, rate: int = RATE) -> None:
        if rate != RATE:
            raise ValueError(f"clap detector expects {RATE} Hz, got {rate}")
        self._tail = np.zeros(0, dtype=np.float32)
        self._floor = FLOOR_MIN
        self._prev_energy = 0.0
        self._t_ms = 0.0                     # stream clock
        self._burst: dict | None = None      # the candidate being measured
        self._claps: deque[float] = deque(maxlen=4)
        self._last_fire = -1e9
        self._window = np.hanning(_WIN).astype(np.float32)

    # -- public ---------------------------------------------------------------
    def reset(self) -> None:
        self._burst = None
        self._claps.clear()

    def feed(self, pcm: bytes) -> bool:
        """True exactly once per completed double clap."""
        if not pcm:
            return False
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        buf = np.concatenate((self._tail, samples)) if self._tail.size else samples
        n_hops = len(buf) // HOP
        self._tail = buf[n_hops * HOP:].copy()
        fired = False
        for i in range(n_hops):
            if self._hop(buf, i * HOP):
                fired = True
        return fired

    # -- one 10 ms hop --------------------------------------------------------
    def _hop(self, buf: np.ndarray, off: int) -> bool:
        hop = buf[off:off + HOP]
        energy = float(np.sqrt(np.mean(hop * hop)) + 1e-12)
        self._t_ms += HOP * 1000.0 / RATE
        fired = False

        if self._burst is None:
            # A candidate needs BOTH a jump over the room's floor and a jump
            # over the hop immediately before it. The second test is what a
            # fixed threshold cannot do: it is what "sharp" means.
            if (energy > self._floor * ONSET_RATIO
                    and energy > self._prev_energy * ATTACK_RATIO
                    and energy > FLOOR_MIN * 4):
                self._burst = {"start": self._t_ms, "peak": energy,
                               "hf": self._hf_ratio(buf, off)}
            else:
                self._track_floor(energy)
        else:
            b = self._burst
            b["peak"] = max(b["peak"], energy)
            # the burst ends when it has decayed well back toward the floor
            if energy < max(b["peak"] * 0.10, self._floor * 3.0):
                fired = self._close_burst(b)
                self._burst = None
            elif self._t_ms - b["start"] > MAX_BURST_MS:
                self._burst = None          # sustained: a voice, not a clap
                self._track_floor(energy)

        self._prev_energy = energy
        return fired

    def _close_burst(self, b: dict) -> bool:
        dur = self._t_ms - b["start"]
        if not (MIN_BURST_MS <= dur <= MAX_BURST_MS):
            return False
        if b["hf"] < HF_MIN:
            return False                    # a thud, not a slap
        return self._register_clap()

    def _register_clap(self) -> bool:
        now = self._t_ms
        if now - self._last_fire < REFRACTORY_MS:
            return False
        # only the most recent clap matters for pairing
        if self._claps and GAP_MIN_MS <= now - self._claps[-1] <= GAP_MAX_MS:
            self._last_fire = now
            self._claps.clear()
            return True
        self._claps.append(now)
        return False

    def _track_floor(self, energy: float) -> None:
        a = _FLOOR_ALPHA if energy < self._floor else _FLOOR_ALPHA_UP
        self._floor = max(FLOOR_MIN, a * self._floor + (1.0 - a) * energy)

    def _hf_ratio(self, buf: np.ndarray, off: int) -> float:
        """Energy above 1.8 kHz as a fraction of the total, on the onset frame.
        A clap is broadband; a door thud and a vowel are not."""
        seg = buf[off:off + _WIN]
        if len(seg) < _WIN:
            seg = np.pad(seg, (0, _WIN - len(seg)))
        spec = np.abs(np.fft.rfft(seg * self._window)) ** 2
        total = float(spec.sum()) + 1e-12
        cut = int(1800 / (RATE / _WIN))
        return float(spec[cut:].sum()) / total


def clap_features(pcm: bytes) -> dict:
    """Diagnostics for one isolated sound — what the detector measured and
    which test rejected it. For tuning against real recordings."""
    det = ClapDetector()
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    peak = float(np.abs(samples).max()) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
    hf = det._hf_ratio(samples, int(np.argmax(np.abs(samples))))
    above = np.abs(samples) > peak * 0.10
    dur = (float(np.flatnonzero(above)[-1] - np.flatnonzero(above)[0]) * 1000.0
           / RATE) if above.any() else 0.0
    return {"peak": round(peak, 4), "rms": round(rms, 5),
            "hf_ratio": round(hf, 3), "burst_ms": round(dur, 1),
            "hf_ok": hf >= HF_MIN,
            "duration_ok": MIN_BURST_MS <= dur <= MAX_BURST_MS}


def db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-12))
