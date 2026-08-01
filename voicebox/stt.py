"""faster-whisper wrapper: PCM16 @ 16 kHz in, text out.

CPU int8 — `small` lands around real-time×2 on a modern x86 core for short
utterances, which is what a conversational exchange produces. The language is
pinned (env VOICEBOX_LANG, default en): autodetection costs an extra pass and
this box serves one operator."""
import os

import numpy as np
from faster_whisper import WhisperModel

# Whisper hallucinates fillers on borderline audio; segments this unconfident
# are dropped rather than spoken back into the conversation as operator words.
NO_SPEECH_MAX = 0.6
MIN_SAMPLES = int(0.25 * 16_000)      # sub-250 ms clips are never real speech


class Transcriber:
    def __init__(self, model_size: str | None = None,
                 download_root: str | None = None) -> None:
        self.model_size = model_size or os.environ.get("VOICEBOX_WHISPER", "small")
        self.lang = os.environ.get("VOICEBOX_LANG", "en")
        self.model = WhisperModel(self.model_size, device="cpu",
                                  compute_type="int8",
                                  download_root=download_root)

    def transcribe(self, pcm: bytes) -> str:
        """Blocking — call from an executor. Returns '' for silence/noise."""
        if len(pcm) < MIN_SAMPLES * 2:
            return ""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self.model.transcribe(
            audio, language=self.lang, beam_size=1, temperature=0.0,
            condition_on_previous_text=False)
        parts = [s.text for s in segments if s.no_speech_prob <= NO_SPEECH_MAX]
        return " ".join(p.strip() for p in parts).strip()
