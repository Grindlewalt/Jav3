"""Audio for the headless voice client: mic capture in, TTS playback out.

Kept apart from agent.py because it is the only part that needs a sound card,
so everything else can be tested on a box that has none.

Two streams, two rates, deliberately not resampled anywhere:

    capture   16 kHz mono PCM16   what the sidecar's VAD and whisper expect
    playback  24 kHz mono PCM16   what the architect voice produces

PortAudio (via sounddevice) opens each at its native rate and the OS handles
the hardware conversion. The browser client does the same thing with two
AudioContexts, for the same reason: a resample in Python would cost latency and
buy nothing.

**Barge-in.** Playback is a queue this module owns, so pausing is a flag rather
than a stop: `pause()` stops feeding frames but keeps the queue, so a false
alarm resumes exactly where it left off. That mirrors the browser's
`AudioContext.suspend()` contract, which is what backend/voice.py already
expects — resume_playback must lose nothing.
"""
from __future__ import annotations

import collections
import logging
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger("voicedesk.audio")

CAPTURE_RATE = 16_000
PLAY_RATE = 24_000
BLOCK_MS = 60                       # matches the browser worklet's batch size
CAPTURE_BLOCK = CAPTURE_RATE * BLOCK_MS // 1000


class Playback:
    """A 24 kHz output stream fed from a queue of PCM16 chunks."""

    def __init__(self, device=None) -> None:
        self._q: collections.deque[np.ndarray] = collections.deque()
        self._lock = threading.Lock()
        self._cur: np.ndarray | None = None
        self._pos = 0
        self._paused = False
        self._played_ms = 0.0            # of the chunk under the playhead
        self._chunk_id = 0
        self._ids: collections.deque[int] = collections.deque()
        self.on_chunk_done = lambda cid: None
        self._stream = sd.OutputStream(
            samplerate=PLAY_RATE, channels=1, dtype="int16",
            blocksize=0, device=device, callback=self._callback)

    def start(self) -> None:
        self._stream.start()

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:  # noqa: BLE001 — shutting down anyway
            pass

    # -- the audio thread ----------------------------------------------------

    def _callback(self, outdata, frames, time_info, status) -> None:
        if status:
            log.debug("playback status: %s", status)
        out = outdata.reshape(-1)
        written = 0
        with self._lock:
            if self._paused:
                out[:] = 0
                return
            while written < frames:
                if self._cur is None:
                    if not self._q:
                        break
                    self._cur = self._q.popleft()
                    self._chunk_id = self._ids.popleft() if self._ids else 0
                    self._pos = 0
                    self._played_ms = 0.0
                take = min(frames - written, len(self._cur) - self._pos)
                out[written:written + take] = self._cur[self._pos:self._pos + take]
                self._pos += take
                written += take
                self._played_ms += take * 1000.0 / PLAY_RATE
                if self._pos >= len(self._cur):
                    done_id = self._chunk_id
                    self._cur = None
                    # never call out from the audio thread; hand it off
                    threading.Thread(target=self.on_chunk_done, args=(done_id,),
                                     daemon=True).start()
        if written < frames:
            out[written:] = 0

    # -- the control thread --------------------------------------------------

    def enqueue(self, chunk_id: int, pcm: bytes) -> None:
        if not pcm:
            return
        samples = np.frombuffer(pcm, dtype="<i2").copy()
        with self._lock:
            self._q.append(samples)
            self._ids.append(chunk_id)

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._paused or self._cur is not None or bool(self._q)

    def position(self) -> tuple[int, int]:
        """(chunk_id, ms of it already heard) — what a barge-in reports."""
        with self._lock:
            return self._chunk_id, int(self._played_ms)

    def pause(self) -> None:
        """Stop output but keep the queue: a false-alarm barge-in resumes with
        nothing lost, which is the contract backend/voice.py relies on."""
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    def stop_all(self) -> None:
        with self._lock:
            self._q.clear()
            self._ids.clear()
            self._cur = None
            self._pos = 0
            self._paused = False

    def chime(self) -> None:
        """Two rising notes — the "I'm listening" cue after a wake word. Same
        pair of frequencies as the browser's, so the two clients sound alike."""
        t = np.arange(int(0.14 * PLAY_RATE)) / PLAY_RATE
        tone = np.zeros_like(t)
        for freq, at in ((740, 0.0), (1109, 0.09)):
            env = np.clip((t - at) / 0.015, 0, 1) * np.exp(-np.maximum(t - at, 0) / 0.05)
            tone += np.sin(2 * np.pi * freq * np.maximum(t - at, 0)) * env
        pcm = (np.clip(tone * 0.12, -1, 1) * 32767).astype("<i2")
        self.enqueue(0, pcm.tobytes())


class Capture:
    """A 16 kHz input stream that hands 60 ms PCM16 blocks to a callback."""

    def __init__(self, on_block, device=None) -> None:
        self._on_block = on_block
        self._stream = sd.InputStream(
            samplerate=CAPTURE_RATE, channels=1, dtype="int16",
            blocksize=CAPTURE_BLOCK, device=device, callback=self._callback)

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("capture status: %s", status)
        block = indata.reshape(-1)
        rms = float(np.sqrt(np.mean((block.astype(np.float32) / 32768.0) ** 2)))
        self._on_block(block.tobytes(), rms)

    def start(self) -> None:
        self._stream.start()

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:  # noqa: BLE001
            pass


def list_devices() -> str:
    """For `--list-devices`: which input/output to name in the config."""
    lines = []
    for i, d in enumerate(sd.query_devices()):
        kinds = []
        if d["max_input_channels"]:
            kinds.append("in")
        if d["max_output_channels"]:
            kinds.append("out")
        lines.append(f"  {i}: {d['name']}  [{'/'.join(kinds)}]")
    return "\n".join(lines)
