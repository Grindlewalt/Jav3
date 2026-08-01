"""Streaming voice-activity detection: silero v5 over 32 ms frames.

pysilero-vad runs the silero ONNX model with onnxruntime alone (no torch) and
bundles the model file, so there is nothing to download for VAD.

`StreamingVAD.feed(pcm)` consumes arbitrary-sized chunks of 16 kHz mono PCM16
and yields events as speech boundaries are crossed:

    ("speech_start", None)        speech confirmed (after START_FRAMES)
    ("speech_end", utterance)     silence held for HANG_FRAMES; `utterance` is
                                  the buffered PCM16 bytes including PRE_ROLL
                                  frames before the trigger so the first
                                  syllable isn't clipped

The thresholds are deliberately asymmetric (easy to stay in speech, harder to
enter it) — a barge-in false positive costs a playback hiccup on the Pi side,
but a clipped utterance start costs a mis-transcription."""
from collections import deque

from pysilero_vad import SileroVoiceActivityDetector

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 512                    # silero v5 window at 16 kHz = 32 ms
FRAME_BYTES = FRAME_SAMPLES * 2

START_PROB = 0.50
KEEP_PROB = 0.35                       # stay-in-speech threshold
START_FRAMES = 2                       # 64 ms of confident speech to trigger
HANG_FRAMES = 13                       # ~416 ms of silence ends the utterance
PRE_ROLL_FRAMES = 8                    # ~256 ms kept from before the trigger
MAX_UTTERANCE_FRAMES = 30 * 1000 // 32  # hard cap ~30 s — flush, don't balloon


class StreamingVAD:
    def __init__(self) -> None:
        self._vad = SileroVoiceActivityDetector()
        self._pending = b""                          # partial frame carry-over
        self._pre = deque(maxlen=PRE_ROLL_FRAMES)    # rolling pre-trigger audio
        self._utterance: list[bytes] = []
        self._in_speech = False
        self._streak = 0                             # consecutive speechy frames
        self._silence = 0                            # consecutive quiet frames

    def reset(self) -> None:
        self._vad.reset()
        self._pending = b""
        self._pre.clear()
        self._utterance = []
        self._in_speech = False
        self._streak = self._silence = 0

    def feed(self, pcm: bytes):
        """Consume PCM16 bytes; yield (event, payload) tuples."""
        self._pending += pcm
        while len(self._pending) >= FRAME_BYTES:
            frame = self._pending[:FRAME_BYTES]
            self._pending = self._pending[FRAME_BYTES:]
            yield from self._step(frame)

    def _step(self, frame: bytes):
        prob = self._vad(frame)
        if not self._in_speech:
            self._pre.append(frame)
            if prob >= START_PROB:
                self._streak += 1
                if self._streak >= START_FRAMES:
                    self._in_speech = True
                    self._silence = 0
                    self._utterance = list(self._pre)
                    self._pre.clear()
                    yield ("speech_start", None)
            else:
                self._streak = 0
        else:
            self._utterance.append(frame)
            if prob >= KEEP_PROB:
                self._silence = 0
            else:
                self._silence += 1
            if (self._silence >= HANG_FRAMES
                    or len(self._utterance) >= MAX_UTTERANCE_FRAMES):
                utterance = b"".join(self._utterance)
                self._in_speech = False
                self._streak = 0
                self._utterance = []
                yield ("speech_end", utterance)
