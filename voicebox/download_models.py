"""Fetch the voicebox model files into $VOICEBOX_MODELS (default ./models).

Idempotent: existing files are kept, so this is safe as a container
entrypoint step. Silero VAD ships inside the pysilero-vad wheel — nothing to
fetch for it.

  kokoro-v1.0.onnx  (~310 MB)  + voices-v1.0.bin (~27 MB)   — kokoro-onnx release
  faster-whisper $VOICEBOX_WHISPER (default: small, ~250 MB) — HF hub
"""
import os
import sys
import urllib.request
from pathlib import Path

KOKORO_BASE = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
               "model-files-v1.0")
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")


def fetch(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  {dest.name}: already present")
        return
    print(f"  {dest.name}: downloading …")
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)   # noqa: S310 — pinned https URLs
    tmp.rename(dest)
    print(f"  {dest.name}: done ({dest.stat().st_size // 1_000_000} MB)")


def main() -> int:
    models = Path(os.environ.get("VOICEBOX_MODELS", "./models"))
    models.mkdir(parents=True, exist_ok=True)

    print("kokoro:")
    for name in KOKORO_FILES:
        fetch(f"{KOKORO_BASE}/{name}", models / name)

    size = os.environ.get("VOICEBOX_WHISPER", "small")
    print(f"faster-whisper ({size}):")
    from faster_whisper.utils import download_model
    download_model(size, cache_dir=str(models / "whisper"))
    print("  done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
