"""faster-whisper wrapper: PCM16 @ 16 kHz in, text + evidence out.

The model is `large-v3-turbo` on the GPU. That is a deliberate change from
`small` (2026-08-09) and it was measured, because clean speech does not show
the difference at all — every size gets a close-mic'd TTS clip right. What
separates them is the operator's ACTUAL condition: a room mic, at a distance,
with a guitar going in the background. Mean WER over 8 utterances x 3 noise
realizations, reverb ~0.45 s RT60 at 0.55 gain, guitar mixed at the stated SNR:

    config                        clean   room   gtr@10  gtr@5  gtr@0   lat
    small / beam1 temp0 (old)     10.8%  19.4%   17.2%  22.6%  26.5%    79 ms
    small / hardened + hotwords    4.9%  13.1%   26.1%  32.9%  42.9%   113 ms
    large-v3-turbo / hardened      4.9%   8.7%   11.3%  12.6%  13.9%   216 ms

Two things to keep in mind before touching any of this:

**The temperature fallback is only safe on a strong model.** Look at the middle
row: hardening `small` HELPS in a quiet room and makes it two to three times
WORSE under a guitar. The fallback fires when a decode trips
`compression_ratio_threshold` / `log_prob_threshold`, and a weak model
re-sampling at temperature 0.4+ on noisy audio invents fluent text instead of
returning the mangled-but-honest version. So the fallback is enabled by MODEL
CLASS, not globally — see `_wants_fallback`.

**The cost is +137 ms and +424 MiB.** Whisper goes 79 -> 216 ms, which puts the
speech-end -> first-audio chain around 1.23 s against the 880 ms target, inside
the "acceptable 1-1.5 s" band. cuda:0 goes to ~4.3 GB of 8, still sharing
comfortably with architect-tts. `VOICEBOX_WHISPER=small` is the rollback and
turns the fallback off with it.

`transcribe()` returns evidence, not just text. The Pi's barge-in decision used
to be "the transcript was non-empty", which is exactly why a guitar could stop
Jarvis mid-sentence: whisper hands back confident-looking words for music. The
caller now gets `no_speech_prob`, `avg_logprob` and the hallucination verdict
and can refuse to treat that as the operator talking.
"""
import logging
import os
import re
from dataclasses import dataclass, field

import numpy as np

# faster_whisper is imported inside Transcriber.__init__, not here: the pure
# helpers below (is_phantom, Result) are what the Pi's barge-in rules are
# tested against, and they must be importable on a box with no CTranslate2,
# no CUDA and no model files.

log = logging.getLogger("voicebox.stt")

# Whisper hallucinates fillers on borderline audio; segments this unconfident
# are dropped rather than spoken back into the conversation as operator words.
NO_SPEECH_MAX = 0.6
MIN_SAMPLES = int(0.25 * 16_000)      # sub-250 ms clips are never real speech

# Below this, a decode is not trustworthy enough to interrupt a reply over.
# Kept separate from NO_SPEECH_MAX (which drops the segment entirely): this one
# only downgrades `confident`, so the words still reach the Pi and can still be
# routed if the session is idle.
MIN_AVG_LOGPROB = -1.0

# Model families that can be trusted with the temperature fallback. See the
# module docstring — on `small` the fallback is actively harmful under noise.
_STRONG = ("large", "turbo", "distil-large", "medium")

# What whisper says when it is handed music, room tone or its own echo. These
# come out fluent, correctly punctuated and completely fabricated, so no
# confidence threshold catches them — they have to be named. Matched against
# the whole transcript, lowercased and stripped of punctuation.
_PHANTOMS = frozenset({
    "you", "thank you", "thanks", "thank you very much", "thanks for watching",
    "thank you for watching", "please subscribe", "subscribe to my channel",
    "like and subscribe", "bye", "bye bye", "goodbye", "okay", "ok", "oh",
    "hmm", "mm", "mhm", "uh", "um", "yeah", "the end", "to be continued",
    "music", "applause", "laughter", "silence", "outro", "intro",
    "subtitles by the amara org community", "transcription by castingwords",
    "amara org", "www mooji org", "copyright", "all rights reserved",
})
# Bracketed sound tags and musical notes are never speech.
_TAG_ONLY = re.compile(r"^[\s\[\(\*♪♫#-]*(music|applause|laughter|silence|"
                       r"sound|noise|inaudible|blank_audio)?[\s\]\)\*♪♫#.-]*$",
                       re.I)
_PUNCT = re.compile(r"[^\w\s']")


def _flatten(text: str) -> str:
    # punctuation becomes a SPACE, not nothing: whisper's subtitle credits
    # arrive as "Subtitles by the Amara.org community", and deleting the dot
    # welds "Amara" to "org" so the denylist entry never matches.
    return " ".join(_PUNCT.sub(" ", text).lower().split())


def is_phantom(text: str) -> bool:
    """True when the transcript is a known whisper fabrication rather than
    something the operator said. Empty text counts."""
    flat = _flatten(text)
    if not flat:
        return True
    if _TAG_ONLY.match(text.strip()):
        return True
    if flat in _PHANTOMS:
        return True
    # The repetition loop: "you you you you", "Thank you. Thank you. Thank
    # you. Thank you." A short phrase repeated four or more times with nothing
    # else in the utterance is a decoder stuck in a cycle, not a sentence. Four
    # and not three, so a real "no, no, no" still gets through as a barge-in.
    words = flat.split()
    for period in range(1, 5):
        if len(words) < 4 * period or len(words) % period:
            continue
        head = words[:period]
        if all(words[i:i + period] == head
               for i in range(period, len(words), period)):
            return True
    return False


@dataclass
class Result:
    """A transcription plus the evidence for trusting it."""
    text: str = ""
    no_speech_prob: float = 1.0
    avg_logprob: float = -10.0
    phantom: bool = True
    dur_ms: int = 0
    dropped: list[str] = field(default_factory=list)   # low-confidence segments

    @property
    def confident(self) -> bool:
        """Strong enough to interrupt a reply over."""
        return (bool(self.text) and not self.phantom
                and self.no_speech_prob <= NO_SPEECH_MAX
                and self.avg_logprob >= MIN_AVG_LOGPROB)

    def as_event(self) -> dict:
        return {"text": self.text, "dur_ms": self.dur_ms,
                "no_speech_prob": round(self.no_speech_prob, 3),
                "avg_logprob": round(self.avg_logprob, 3),
                "phantom": self.phantom, "confident": self.confident}


def _wants_fallback(model_size: str) -> bool:
    override = os.environ.get("VOICEBOX_TEMP_FALLBACK", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    return any(s in model_size.lower() for s in _STRONG)


class Transcriber:
    def __init__(self, model_size: str | None = None,
                 download_root: str | None = None) -> None:
        self.model_size = model_size or os.environ.get("VOICEBOX_WHISPER",
                                                       "large-v3-turbo")
        self.lang = os.environ.get("VOICEBOX_LANG", "en")
        # VOICEBOX_DEVICE=cuda puts whisper on the GPU. Needs onnxruntime-gpu's
        # sibling wheels — see requirements.txt — and LD_LIBRARY_PATH at the
        # pip nvidia libs.
        device = os.environ.get("VOICEBOX_DEVICE", "cpu")
        # int8_float16 is the right default for the turbo model sharing cuda:0
        # with architect-tts: 1201 MiB resident, and it measured no worse than
        # float16 on the noise sweep.
        compute = os.environ.get(
            "VOICEBOX_COMPUTE",
            "int8_float16" if device == "cuda" else "int8")
        self.beam_size = int(os.environ.get("VOICEBOX_BEAM", "5"))
        self.fallback = _wants_fallback(self.model_size)
        # Live vocabulary, pushed down the socket by the Pi (which is the side
        # that knows the music library, project slugs and people's names). The
        # sidecar holds no Jarvis state of its own — this is a cache, refreshed
        # on every connect, and an empty one just means no bias.
        self._hotwords: str | None = None
        from faster_whisper import WhisperModel
        self.model = WhisperModel(self.model_size, device=device,
                                  compute_type=compute,
                                  download_root=download_root)
        # `hotwords` arrived in faster-whisper 1.0.2. Passing it to an older
        # build is a TypeError on EVERY utterance — voice would be completely
        # dead rather than merely un-biased, and this box is deployed by hand
        # on a machine the agent cannot restart. Ask the signature instead of
        # assuming the requirements file was honoured.
        import inspect
        self._can_hotword = "hotwords" in inspect.signature(
            self.model.transcribe).parameters
        if not self._can_hotword:
            log.warning("this faster-whisper has no `hotwords` support "
                        "(needs >= 1.0.2) — the vocabulary push will be "
                        "accepted and ignored")
        log.info("whisper %s on %s/%s, beam=%d, temp-fallback=%s, hotwords=%s",
                 self.model_size, device, compute, self.beam_size,
                 "on" if self.fallback else "off",
                 "yes" if self._can_hotword else "NO")

    def set_vocab(self, words: list[str]) -> int:
        """Bias decoding toward names the operator actually says. Returns the
        number of terms accepted. Whisper takes this as a prompt-shaped string,
        so it is length-capped — an over-long bias list starts costing accuracy
        on ordinary words."""
        terms, seen = [], set()
        for w in words:
            w = " ".join(str(w).split())[:60]
            key = w.lower()
            if w and key not in seen:
                seen.add(key)
                terms.append(w)
        joined = ", ".join(terms)
        if len(joined) > 900:
            joined = joined[:900].rsplit(",", 1)[0]
            terms = [t for t in terms if t in joined]
        self._hotwords = joined or None
        log.info("vocab set: %d terms, %d chars", len(terms), len(joined))
        return len(terms)

    def transcribe(self, pcm: bytes) -> Result:
        """Blocking — call from an executor."""
        dur_ms = len(pcm) // 32
        if len(pcm) < MIN_SAMPLES * 2:
            return Result(dur_ms=dur_ms)
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        temperature = ([0.0, 0.2, 0.4, 0.6, 0.8, 1.0] if self.fallback
                       else [0.0])
        extra = {"hotwords": self._hotwords} if self._can_hotword else {}
        segments, _info = self.model.transcribe(
            audio, language=self.lang, beam_size=self.beam_size,
            temperature=temperature,
            # These two are what make the fallback fire at all: a decode that
            # loops ("you you you") blows the compression ratio, and one that
            # is guessing blows the log prob.
            compression_ratio_threshold=2.4,
            log_prob_threshold=MIN_AVG_LOGPROB,
            no_speech_threshold=NO_SPEECH_MAX,
            # Never carry context between utterances: it is the single largest
            # source of whisper drifting into invented continuations.
            condition_on_previous_text=False,
            **extra)

        kept, dropped, weights = [], [], []
        no_speech, logprob = [], []
        for s in segments:
            if s.no_speech_prob > NO_SPEECH_MAX:
                dropped.append(s.text.strip())
                continue
            kept.append(s.text.strip())
            # weight the per-utterance averages by segment length: a half-second
            # tail must not outvote three seconds of clear speech
            w = max(s.end - s.start, 0.05)
            weights.append(w)
            no_speech.append(s.no_speech_prob * w)
            logprob.append(s.avg_logprob * w)

        text = " ".join(p for p in kept if p).strip()
        total = sum(weights) or 1.0
        res = Result(
            text=text,
            no_speech_prob=(sum(no_speech) / total) if weights else 1.0,
            avg_logprob=(sum(logprob) / total) if weights else -10.0,
            phantom=is_phantom(text),
            dur_ms=dur_ms,
            dropped=[d for d in dropped if d])
        if res.phantom and text:
            log.info("phantom transcript discarded: %r", text[:120])
        return res
