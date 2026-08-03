"""TTS client: text in, PCM16 @ 24 kHz out — the "architect" voice.

The voice itself runs in a separate process (`architect-tts`): it needs torch
+ chatterbox on Python 3.12, while this sidecar runs Python 3.14 for
faster-whisper/silero/openwakeword. They cannot share an interpreter, so the
voice is reached over loopback HTTP instead. The transfer is free next to
synthesis — a 2 s clause is 96 kB and lands in well under a millisecond on lo.

The module surface is unchanged from the kokoro implementation this replaced
(SAMPLE_RATE, PIECE_CHARS, split_tts_text, models_dir, Synth.synth), so
app.py needs no edit.
"""
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

SAMPLE_RATE = 24_000

# The synth generates a text in one blocking pass, so a long chunk would sit
# silent for its whole synth time before the first slice streams. Texts are
# pre-split at clause boundaries into pieces this size; each piece streams as
# soon as ITS synth lands, which is what makes the TTS feel on-the-fly. Also
# the grain at which a cancel takes effect.
PIECE_CHARS = 120

log = logging.getLogger("voicebox.tts")


def split_tts_text(text: str) -> list[str]:
    """Clause-boundary split into pieces ≤ ~PIECE_CHARS (best effort)."""
    text = " ".join(text.split())
    if len(text) <= PIECE_CHARS:
        return [text] if text else []
    pieces: list[str] = []
    rest = text
    while len(rest) > PIECE_CHARS:
        window = rest[:PIECE_CHARS]
        cut = -1
        for sep in (". ", "! ", "? ", "; ", ": ", ", ", " "):
            cut = window.rfind(sep)
            if cut > PIECE_CHARS // 3:      # don't strand a tiny lead piece
                cut += len(sep) - 1         # keep the punctuation on the left
                break
        if cut <= 0:
            cut = PIECE_CHARS
        pieces.append(rest[:cut + 1].strip())
        rest = rest[cut + 1:].strip()
    if rest:
        pieces.append(rest)
    return [p for p in pieces if p]


def models_dir() -> Path:
    return Path(os.environ.get("VOICEBOX_MODELS", "./models"))


class Synth:
    """Talks to the architect-tts service. Construction is cheap — the model
    lives in that process and is loaded once at ITS startup, so restarting
    this sidecar costs nothing in TTS warm-up."""

    def __init__(self, url: str | None = None, timeout: float | None = None) -> None:
        self.url = (url or os.environ.get("VOICEBOX_TTS_URL",
                                          "http://127.0.0.1:8123")).rstrip("/")
        self.timeout = timeout or float(os.environ.get("VOICEBOX_TTS_TIMEOUT", "30"))
        self.voice = "architect"
        # A health probe here turns "the voice service is down" into one clear
        # log line at startup instead of a failure on the operator's first word.
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=5) as r:
                info = json.load(r)
            if info.get("sample_rate") != SAMPLE_RATE:
                raise RuntimeError(
                    f"architect-tts is {info.get('sample_rate')} Hz, "
                    f"expected {SAMPLE_RATE}")
            self.voice = info.get("voice") or self.voice
            log.info("architect-tts ready at %s (%s, %s Hz)",
                     self.url, info.get("device"), info.get("sample_rate"))
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            log.warning("architect-tts not reachable at %s (%s) — TTS will "
                        "fail until it is up", self.url, exc)

    def synth(self, text: str) -> bytes:
        """Blocking — call from an executor. Returns PCM16 mono @ 24 kHz."""
        text = " ".join(text.split())
        if not text:
            return b""
        req = urllib.request.Request(
            f"{self.url}/synth",
            json.dumps({"text": text}).encode(),
            {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read()
