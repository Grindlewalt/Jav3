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
        # VOICEBOX_DEVICE=cuda puts whisper on the GPU (72 ms/utterance on a
        # 3060 Ti vs ~2 s CPU). Needs onnxruntime-gpu's sibling wheels — see
        # requirements.txt — and LD_LIBRARY_PATH at the pip nvidia libs.
        device = os.environ.get("VOICEBOX_DEVICE", "cpu")
        # VOICEBOX_COMPUTE overrides: int8_float16 halves whisper's VRAM
        # (~0.7GB vs ~1.3GB) at no meaningful accuracy cost — the right pick
        # when the LLM shares the same card.
        compute = os.environ.get(
            "VOICEBOX_COMPUTE", "float16" if device == "cuda" else "int8")
        self.model = WhisperModel(self.model_size, device=device,
                                  compute_type=compute,
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
