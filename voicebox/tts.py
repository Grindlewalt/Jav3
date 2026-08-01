"""Kokoro TTS wrapper: text in, PCM16 @ 24 kHz out.

kokoro-onnx runs the 82M Kokoro model on onnxruntime CPU; a sentence-sized
chunk synthesizes in a few hundred ms. Model files are fetched by
download_models.py into $VOICEBOX_MODELS."""
import os
from pathlib import Path

import numpy as np
from kokoro_onnx import Kokoro

SAMPLE_RATE = 24_000
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"

# kokoro synthesizes a text in one blocking pass, so a long chunk would sit
# silent for its whole synth time before the first slice streams. Texts are
# pre-split at clause boundaries into pieces this size; each piece streams as
# soon as ITS synth lands (~sub-second), which is what makes the TTS feel
# on-the-fly. Also the grain at which a cancel takes effect.
PIECE_CHARS = 120


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
    def __init__(self, voice: str | None = None, speed: float | None = None) -> None:
        d = models_dir()
        self.voice = voice or os.environ.get("VOICEBOX_VOICE", "bm_lewis")
        self.speed = speed or float(os.environ.get("VOICEBOX_SPEED", "1.0"))
        # bm_* voices are British English; kokoro needs the matching lang code
        self.lang = os.environ.get(
            "VOICEBOX_TTS_LANG",
            "en-gb" if self.voice.startswith("b") else "en-us")
        self.kokoro = Kokoro(str(d / MODEL_FILE), str(d / VOICES_FILE))

    def synth(self, text: str) -> bytes:
        """Blocking — call from an executor. Returns PCM16 mono @ 24 kHz."""
        samples, sr = self.kokoro.create(
            text, voice=self.voice, speed=self.speed, lang=self.lang)
        if sr != SAMPLE_RATE:  # kokoro is fixed at 24 kHz; guard the contract
            raise RuntimeError(f"kokoro returned {sr} Hz, expected {SAMPLE_RATE}")
        clipped = np.clip(samples, -1.0, 1.0)
        return (clipped * 32767.0).astype("<i2").tobytes()
