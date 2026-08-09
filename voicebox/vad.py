"""Streaming voice-activity detection: silero v5 over 32 ms frames.

pysilero-vad runs the silero ONNX model with onnxruntime alone (no torch) and
bundles the model file, so there is nothing to download for VAD.

`StreamingVAD.feed(pcm)` consumes arbitrary-sized chunks of 16 kHz mono PCM16
and yields events as speech boundaries are crossed:

    ("speech_start", None)        speech confirmed (after START_FRAMES)
    ("speech_end", Utterance)     silence held for HANG_FRAMES; the Utterance
                                  carries the buffered PCM16 including PRE_ROLL
                                  frames before the trigger (so the first
                                  syllable isn't clipped) AND the silero
                                  evidence for it being speech at all

The thresholds are deliberately asymmetric (easy to stay in speech, harder to
enter it) — a barge-in false positive costs a playback hiccup on the Pi side,
but a clipped utterance start costs a mis-transcription.

**Why the utterance carries statistics.** An amplitude gate cannot tell a
guitar from a person, which is how a brother playing in the same room could
stop Jarvis mid-sentence: the browser's RMS VAD trips, whisper is handed music
and returns confident-looking words, and the Pi treated non-empty text as the
operator talking. Silero is a speech model, not a loudness meter — sustained
music sits low and erratic where speech sits high and steady — so the mean
probability and the above-threshold ratio over the utterance are real evidence.
They ride along with the audio and the Pi decides (see backend/voice.py)."""
import os
from collections import deque
from dataclasses import dataclass, field

from pysilero_vad import SileroVoiceActivityDetector

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 512                    # silero v5 window at 16 kHz = 32 ms
FRAME_BYTES = FRAME_SAMPLES * 2

START_PROB = 0.50
KEEP_PROB = 0.35                       # stay-in-speech threshold
START_FRAMES = 2                       # 64 ms of confident speech to trigger

# How long a pause ends the utterance — the single biggest end-of-speech
# latency knob, and it is ADAPTIVE, because a single number cannot be right.
#
# At a flat 300 ms it split ordinary sentences at their internal pauses: "Jarvis,
# put Mockingbird on" ended at the comma, whisper was handed one second of audio
# and returned "Jarvis, Kickstart", and the operator got an answer to something
# they never said. Measured on 12 sentences through the real VAD:
#
#     flat 300 ms      1/12 split    80 ms median endpoint latency
#     flat 500 ms      0/12 split   272 ms
#     adaptive         0/12 split    92 ms
#
# The insight is that the pauses which cause splits follow a SHORT lead-in
# ("Jarvis,", "So,", "Actually,") while a real end-of-turn arrives after a
# whole sentence. So a brief utterance has to hold its silence longer to be
# considered finished, and a substantial one ends on the snappy timer. That
# buys the flat-500 correctness for +12 ms instead of +192 ms.
#
# The cost lands only on genuinely short commands ("Stop.", "Louder."), which
# wait the long hangover — and those are barge-ins, where playback has already
# been paused locally by the browser, so the extra 200 ms is not heard.
HANG_FRAMES = max(3, int(os.environ.get("VOICEBOX_HANG_MS", "300")) // 32)
HANG_FRAMES_LONG = max(HANG_FRAMES,
                       int(os.environ.get("VOICEBOX_HANG_LONG_MS", "500")) // 32)
# Below this much speech, an utterance is treated as a lead-in that is probably
# not finished. 1200 ms was the shortest value with margin — 800 ms still split.
STANDALONE_MS = int(os.environ.get("VOICEBOX_STANDALONE_MS", "1200"))

PRE_ROLL_FRAMES = 8                    # ~256 ms kept from before the trigger
MAX_UTTERANCE_FRAMES = 30 * 1000 // 32  # hard cap ~30 s — flush, don't balloon


@dataclass
class Utterance:
    """Buffered speech plus the evidence that it IS speech."""
    pcm: bytes = b""
    probs: list[float] = field(default_factory=list)   # per-frame, trigger on

    def __len__(self) -> int:                # some callers sort utterances by size
        return len(self.pcm)

    @property
    def mean_prob(self) -> float:
        return (sum(self.probs) / len(self.probs)) if self.probs else 0.0

    @property
    def speech_ratio(self) -> float:
        """Fraction of frames silero considered speech. Sustained talking runs
        high; a guitar or a passing noise burst is spiky and sits low."""
        if not self.probs:
            return 0.0
        return sum(1 for p in self.probs if p >= KEEP_PROB) / len(self.probs)

    def as_event(self) -> dict:
        return {"speech_ratio": round(self.speech_ratio, 3),
                "mean_prob": round(self.mean_prob, 3),
                "frames": len(self.probs)}


class StreamingVAD:
    def __init__(self) -> None:
        self._vad = SileroVoiceActivityDetector()
        self._pending = b""                          # partial frame carry-over
        self._pre = deque(maxlen=PRE_ROLL_FRAMES)    # rolling pre-trigger audio
        self._utterance: list[bytes] = []
        self._probs: list[float] = []
        self._in_speech = False
        self._streak = 0                             # consecutive speechy frames
        self._silence = 0                            # consecutive quiet frames

    def reset(self) -> None:
        self._vad.reset()
        self._pending = b""
        self._pre.clear()
        self._utterance = []
        self._probs = []
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
                    self._probs = []
                    self._pre.clear()
                    yield ("speech_start", None)
            else:
                self._streak = 0
        else:
            self._utterance.append(frame)
            self._probs.append(prob)
            if prob >= KEEP_PROB:
                self._silence = 0
            else:
                self._silence += 1
            # a lead-in has to hold its silence longer than a whole sentence
            # does before we believe it is finished (see HANG_FRAMES above)
            spoken_ms = len(self._utterance) * 32
            hang = (HANG_FRAMES if spoken_ms >= STANDALONE_MS
                    else HANG_FRAMES_LONG)
            if (self._silence >= hang
                    or len(self._utterance) >= MAX_UTTERANCE_FRAMES):
                utterance = Utterance(pcm=b"".join(self._utterance),
                                      probs=list(self._probs))
                self._in_speech = False
                self._streak = 0
                self._utterance = []
                self._probs = []
                yield ("speech_end", utterance)
