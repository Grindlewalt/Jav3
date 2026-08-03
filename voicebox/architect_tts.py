"""architect-tts — the "architect" voice as a standalone HTTP service.

Why a service and not an import: the voice is a Chatterbox Nano zero-shot
clone that needs torch 2.6 + chatterbox on Python 3.12, while voicebox runs
Python 3.14 (faster-whisper, silero, openwakeword). Those two dependency sets
cannot share an interpreter, so the voice gets its own venv and speaks PCM
over loopback. voicebox/tts.py is the client.

  POST /synth   {"text": "..."}  -> audio/L16 PCM16 mono @ 24 kHz (whole clause)
  POST /stream  {"text": "..."}  -> the same, chunked piece by piece
  GET  /health

The model is loaded and warmed at startup and never unloaded, so a request
pays only generation (~6x realtime; a 2 s clause in ~350 ms).

Runs from the voice pack's venv, with the pack on PYTHONPATH:
  VOICE_DEVICE=cuda:1 PYTHONPATH=/home/claude/voice-pack \
    /home/claude/voice-pack/.venv/bin/uvicorn architect_tts:app --port 8123
"""
import asyncio
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

sys.path.insert(0, os.environ.get("VOICE_PACK", "/home/claude/voice-pack"))
from nano_voice import SAMPLE_RATE, Synth, split_text  # noqa: E402

log = logging.getLogger("architect-tts")


@asynccontextmanager
async def lifespan(app: FastAPI):
    t0 = time.time()
    # one model on one GPU, and Synth is not reentrant -> exactly one worker
    app.state.pool = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    app.state.synth = await loop.run_in_executor(app.state.pool, Synth)
    # the first generate() pays kernel autotune; spend it here, not on the
    # operator's first word
    await loop.run_in_executor(app.state.pool, app.state.synth.warmup)
    app.state.ready_s = time.time() - t0
    log.info("architect voice ready in %.1fs on %s",
             app.state.ready_s, app.state.synth.device)
    yield
    app.state.pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="architect-tts", lifespan=lifespan)


class Say(BaseModel):
    text: str


@app.get("/health")
async def health():
    s = getattr(app.state, "synth", None)
    return {"ok": s is not None, "sample_rate": SAMPLE_RATE,
            "device": getattr(s, "device", None),
            "voice": "architect",
            "load_s": round(getattr(app.state, "ready_s", 0.0), 2)}


async def _synth(text: str) -> bytes:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(app.state.pool, app.state.synth.synth, text)


@app.post("/synth")
async def synth(say: Say):
    t0 = time.time()
    pcm = await _synth(say.text)
    return Response(content=pcm, media_type="audio/L16",
                    headers={"X-Gen-Seconds": f"{time.time() - t0:.3f}",
                             "X-Audio-Seconds": f"{len(pcm) / 2 / SAMPLE_RATE:.3f}"})


@app.post("/stream")
async def stream(say: Say):
    """Piece at a time, so the caller can start playing the first clause while
    the rest is still generating."""
    pieces = split_text(say.text)

    async def gen():
        for p in pieces:
            yield await _synth(p)

    return StreamingResponse(gen(), media_type="audio/L16")
