"""voicebox — the Jarvis voice inference sidecar.

Runs on an x86 box next to the Pi (the Pi cannot do audio ML). One privileged
client — the Jarvis backend — connects over a single WebSocket and speaks a
tiny mixed protocol:

  binary up    0x01 | PCM16 mono @ 16 kHz     mic audio, any chunking
  binary down  0x02 | uint32le tts_id | PCM16 mono @ 24 kHz   synthesized audio
  text  up     {"type": "tts", "id": N, "text": "..."}
               {"type": "tts_cancel"}          drop queued + abort current TTS
               {"type": "reset"}               drop any partial utterance state
  text  down   {"type": "ready", "stt": ..., "tts": ...}
               {"type": "speech_start"} / {"type": "speech_end"}
               {"type": "transcript", "text": "...", "dur_ms": D}   '' = noise
               {"type": "tts_start", "id": N} / {"type": "tts_done", "id": N, "dur_ms": D}
               {"type": "error", "message": "..."}

The box holds no Jarvis state and no secrets beyond its own bearer token:
audio in, text out; text in, audio out. Auth follows the computeruse pairing
pattern — compare_digest plus a 1 s penalty sleep on failure. A second
connection supersedes the first (a reconnecting Pi must not be locked out by
its own half-dead socket).
"""
import asyncio
import json
import logging
import os
import secrets
import struct
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket

from stt import Transcriber
from tts import SAMPLE_RATE as TTS_RATE, Synth, models_dir
from vad import StreamingVAD

log = logging.getLogger("voicebox")

MIC_FRAME = 0x01
TTS_FRAME = 0x02
SLICE_BYTES = TTS_RATE                 # rate×2B×0.5s: half a second per slice
                                       # (the grain at which a cancel can land)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.token = os.environ.get("VOICEBOX_TOKEN", "")
    if not app.state.token:
        log.warning("VOICEBOX_TOKEN is not set — all connections will be refused")
    # STT and TTS each get a lane so a transcription never queues behind a
    # synthesis; model loads share the same two threads at startup.
    app.state.executor = ThreadPoolExecutor(max_workers=2)
    loop = asyncio.get_running_loop()
    whisper_dir = str(models_dir() / "whisper")
    stt_f = loop.run_in_executor(app.state.executor, Transcriber, None, whisper_dir)
    tts_f = loop.run_in_executor(app.state.executor, Synth)
    app.state.stt = await stt_f
    app.state.tts = await tts_f
    app.state.client: WebSocket | None = None
    log.info("models loaded: whisper=%s voice=%s",
             app.state.stt.model_size, app.state.tts.voice)
    yield
    app.state.executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="voicebox", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True,
            "stt": getattr(app.state, "stt", None) and app.state.stt.model_size,
            "tts": getattr(app.state, "tts", None) and app.state.tts.voice,
            "token_configured": bool(app.state.token),
            "client_connected": app.state.client is not None}


def _authorized(ws: WebSocket) -> bool:
    token = ws.app.state.token
    supplied = (ws.headers.get("authorization") or "")
    supplied = supplied.removeprefix("Bearer ").strip()
    return bool(token) and secrets.compare_digest(supplied, token)


@app.websocket("/ws")
async def voice_ws(ws: WebSocket):
    if not _authorized(ws):
        await asyncio.sleep(1)          # blunt the guessing rate
        await ws.close(code=4401)
        return
    await ws.accept()

    prev = ws.app.state.client
    if prev is not None:
        # the Pi reconnected before its old socket died — supersede it
        try:
            await prev.close(code=4000)
        except Exception:  # noqa: BLE001 — the old socket may be long gone
            pass
    ws.app.state.client = ws

    loop = asyncio.get_running_loop()
    ex = ws.app.state.executor
    stt: Transcriber = ws.app.state.stt
    tts: Synth = ws.app.state.tts

    vad = StreamingVAD()
    stt_q: asyncio.Queue[bytes] = asyncio.Queue()
    tts_q: asyncio.Queue[tuple[int, int, str]] = asyncio.Queue()  # (gen, id, text)
    gen = 0                              # bumped by tts_cancel: stale = silent

    async def send(obj: dict) -> None:
        await ws.send_text(json.dumps(obj))

    async def stt_worker() -> None:
        # one lane, in order: a fast second utterance must not overtake the
        # verdict for the first (the Pi's barge-in logic depends on ordering)
        while True:
            pcm = await stt_q.get()
            try:
                text = await loop.run_in_executor(ex, stt.transcribe, pcm)
            except Exception as exc:  # noqa: BLE001 — keep the lane alive
                log.exception("stt failed")
                await send({"type": "error", "message": f"stt: {exc}"})
                text = ""
            await send({"type": "transcript", "text": text,
                        "dur_ms": len(pcm) // 32})

    async def tts_worker() -> None:
        nonlocal gen
        while True:
            g, tts_id, text = await tts_q.get()
            if g != gen:                 # cancelled while queued
                continue
            await send({"type": "tts_start", "id": tts_id})
            try:
                pcm = await loop.run_in_executor(ex, tts.synth, text)
            except Exception as exc:  # noqa: BLE001 — keep the lane alive
                log.exception("tts failed")
                await send({"type": "error", "message": f"tts: {exc}"})
                continue
            header = struct.pack("<BI", TTS_FRAME, tts_id)
            for off in range(0, len(pcm), SLICE_BYTES):
                if g != gen:             # cancelled mid-stream: stop between slices
                    break
                await ws.send_bytes(header + pcm[off:off + SLICE_BYTES])
            if g == gen:
                await send({"type": "tts_done", "id": tts_id,
                            "dur_ms": len(pcm) * 1000 // (TTS_RATE * 2)})

    workers = [asyncio.create_task(stt_worker()),
               asyncio.create_task(tts_worker())]
    await send({"type": "ready", "stt": stt.model_size,
                "tts": f"kokoro/{tts.voice}"})
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                if data[:1] == bytes([MIC_FRAME]):
                    for ev, payload in vad.feed(data[1:]):
                        if ev == "speech_start":
                            await send({"type": "speech_start"})
                        else:
                            await send({"type": "speech_end"})
                            stt_q.put_nowait(payload)
                continue
            try:
                cmd = json.loads(msg.get("text") or "{}")
            except ValueError:
                await send({"type": "error", "message": "bad json"})
                continue
            kind = cmd.get("type")
            if kind == "tts":
                tts_q.put_nowait((gen, int(cmd.get("id", 0)),
                                  str(cmd.get("text", ""))))
            elif kind == "tts_cancel":
                gen += 1                 # queued + in-flight all go stale
            elif kind == "reset":
                vad.reset()
            else:
                await send({"type": "error", "message": f"unknown: {kind}"})
    except Exception:  # noqa: BLE001 — a dead socket is a normal ending
        pass
    finally:
        for w in workers:
            w.cancel()
        if ws.app.state.client is ws:
            ws.app.state.client = None
