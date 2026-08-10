"""Fetch the voicebox model files into $VOICEBOX_MODELS (default ./models).

Idempotent: existing files are kept, so this is safe as a container
entrypoint step. Silero VAD ships inside the pysilero-vad wheel — nothing to
fetch for it, and the TTS is a separate service with its own venv and its own
weights (architect_tts.py), so nothing to fetch for that either.

  faster-whisper $VOICEBOX_WHISPER (default: large-v3-turbo, ~1.6 GB) — HF hub

The default MUST track stt.py's, or provisioning downloads one model and the
app then loads a different one — which on a box with no HF access at runtime
is a sidecar that starts and cannot transcribe.
"""
import os
import sys
from pathlib import Path

# Keep in step with stt.py's VOICEBOX_WHISPER default.
DEFAULT_WHISPER = "large-v3-turbo"


def main() -> int:
    models = Path(os.environ.get("VOICEBOX_MODELS", "./models"))
    models.mkdir(parents=True, exist_ok=True)

    size = os.environ.get("VOICEBOX_WHISPER", DEFAULT_WHISPER)
    print(f"faster-whisper ({size}):")
    from faster_whisper.utils import download_model
    download_model(size, cache_dir=str(models / "whisper"))
    print("  done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
