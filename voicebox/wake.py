"""Wake-word detection: openWakeWord's pretrained "hey jarvis" model over the
same 16 kHz mic stream the VAD watches. ~10 ms of CPU per 80 ms frame.

Validated thresholds (kokoro-synthesized speech, weakest voice): positives
score 0.40-1.00, negatives ("hey Marvin", ordinary sentences) under 0.005 —
0.35 splits them with two orders of magnitude of margin, and real human
speech scores higher than TTS.

openwakeword note: 0.6 is required (0.4 lacks the onnx inference path), and
on Python 3.14 it must be installed WITHOUT deps — its tflite-runtime
requirement has no 3.14 wheels and the onnx path never imports it:
    pip install --no-deps "openwakeword==0.6.0" && pip install scipy tqdm requests
"""
import os
import time

import numpy as np

FRAME_SAMPLES = 1280            # openwakeword's native 80 ms @ 16 kHz
REFRACTORY_S = 2.0


class WakeDetector:
    def __init__(self, name: str) -> None:
        from openwakeword import utils
        from openwakeword.model import Model
        utils.download_models(model_names=[name])   # idempotent
        self.name = name
        self.model = Model(wakeword_models=[name], inference_framework="onnx")
        self.threshold = float(os.environ.get("VOICEBOX_WAKE_THRESHOLD", "0.35"))
        self._pending = b""
        self._quiet_until = 0.0

    def feed(self, pcm: bytes) -> bool:
        """Consume PCM16 bytes; True exactly once per detection."""
        self._pending += pcm
        fired = False
        while len(self._pending) >= FRAME_SAMPLES * 2:
            frame = np.frombuffer(self._pending[:FRAME_SAMPLES * 2], np.int16)
            self._pending = self._pending[FRAME_SAMPLES * 2:]
            score = max(self.model.predict(frame).values())
            if score >= self.threshold and time.monotonic() >= self._quiet_until:
                self._quiet_until = time.monotonic() + REFRACTORY_S
                self.model.reset()
                fired = True
        return fired
