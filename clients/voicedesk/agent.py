"""voicedesk — Jarvis desktop mode without a browser.

A background service on the operator's machine: microphone and speaker always
up, "hey Jarvis" to start, and the same conversation the /voice page has. It
speaks the SAME protocol as that page (see backend/voice.py's docstring), so
backend/voice.py does not know or care which client is attached — one state
machine, two front ends.

    mic ──0x01|PCM16@16k──▶  /api/voice/ws  ──0x02|id|PCM16@24k──▶ speaker
                             bearer token

Why not just leave a browser tab open: a tab dies with the browser, needs a
logged-in session, throttles in the background on some platforms, and cannot
start itself at login. This does not.

What it deliberately does NOT do: any audio ML. No wake word, no VAD worth the
name, no transcription — all of that is the sidecar's, reached through the Pi.
This process is a microphone, a speaker and a socket. That keeps the one
machine sitting in the operator's room as dumb as possible.

The local barge-in gate is the exception, and it is only a hair trigger for
latency: the instant the mic goes loud during playback we pause locally and
report it, then the Pi's verdict (which has silero and whisper behind it) says
resume or stop. A false positive costs a sub-second hiccup; the real decision
is made where the evidence is.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import struct
import sys
from pathlib import Path

import websockets

log = logging.getLogger("voicedesk")

MIC_FRAME = 0x01
TTS_FRAME = 0x02

# The browser's numbers (frontend/src/voiceAudio.js), kept identical so the two
# clients behave the same. The bar rises while audio is playing because some of
# our own speech comes back down the mic — a headless box has no
# echoCancellation, so if anything this wants to be less trigger-happy, not
# more. The sidecar rejects music and noise anyway; over-triggering here only
# costs a pause.
VAD_BASE = 0.02
VAD_PLAYING_MULT = 3
VAD_TRIP_BLOCKS = 2


class VoiceDesk:
    def __init__(self, url: str, token: str, *, name: str = "desk",
                 in_device=None, out_device=None, project: str | None = None):
        self.url = url
        self.token = token
        self.name = name
        self.project = project
        self.in_device = in_device
        self.out_device = out_device
        self.ws = None
        self.play = None
        self.cap = None
        self.muted = False
        self._hot = 0
        self._suspended = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._outbox: asyncio.Queue = asyncio.Queue(maxsize=200)

    # ---- audio callbacks (these run on PortAudio threads) -------------------

    def _on_mic(self, pcm: bytes, rms: float) -> None:
        """Hot path. Does no I/O — hands the frame to the asyncio loop and
        returns, because blocking here is an audible glitch."""
        if not self.muted:
            self._post(bytes([MIC_FRAME]) + pcm)
        self._vad(rms)

    def _vad(self, rms: float) -> None:
        playing = self.play is not None and self.play.playing
        gate = VAD_BASE * (VAD_PLAYING_MULT if playing else 1)
        if self.muted or rms < gate:
            self._hot = 0
            return
        self._hot += 1
        if self._hot == VAD_TRIP_BLOCKS and playing and not self._suspended:
            self._suspended = True
            self.play.pause()
            cid, ms = self.play.position()
            self._post(json.dumps({"type": "barge_in", "chunk_id": cid,
                                   "played_ms": ms}))

    def _on_chunk_done(self, chunk_id: int) -> None:
        if chunk_id:
            self._post(json.dumps({"type": "chunk_played",
                                   "chunk_id": chunk_id}))

    def _post(self, item) -> None:
        """Thread-safe hand-off to the socket task. Drops rather than blocks:
        if the socket has stalled, the newest mic audio is worth more than a
        backlog of old audio, and PortAudio must never wait on us."""
        loop = self._loop
        if loop is None:
            return
        def put():
            with contextlib.suppress(asyncio.QueueFull):
                self._outbox.put_nowait(item)
        loop.call_soon_threadsafe(put)

    # ---- the socket ---------------------------------------------------------

    async def run(self) -> None:
        from .audio import Capture, Playback

        self._loop = asyncio.get_running_loop()
        self.play = Playback(device=self.out_device)
        self.play.on_chunk_done = self._on_chunk_done
        self.play.start()
        self.cap = Capture(self._on_mic, device=self.in_device)
        self.cap.start()
        log.info("audio up: mic in, speaker out")

        delay = 1
        try:
            while True:
                try:
                    await self._session()
                    delay = 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — reconnect covers it
                    log.warning("connection lost: %s", exc)
                self.play.stop_all()
                self._suspended = False
                log.info("reconnecting in %ss", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        finally:
            self.cap.close()
            self.play.close()

    async def _session(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        async with websockets.connect(self.url, additional_headers=headers,
                                      ping_interval=20, ping_timeout=20,
                                      max_size=2 ** 20) as ws:
            self.ws = ws
            log.info("connected to %s", self.url)
            hello = {"type": "hello", "tab": self.name}
            if self.project:
                hello["project"] = self.project
                hello["project_mode"] = "pin"
            await ws.send(json.dumps(hello))
            # drain anything the mic queued while we were down
            while not self._outbox.empty():
                self._outbox.get_nowait()

            pump = asyncio.create_task(self._pump(ws))
            try:
                async for msg in ws:
                    if isinstance(msg, bytes):
                        self._on_binary(msg)
                    else:
                        await self._on_json(json.loads(msg))
            finally:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
                self.ws = None

    async def _pump(self, ws) -> None:
        """One task owns the socket's write side, so the audio threads never
        touch it."""
        while True:
            item = await self._outbox.get()
            if isinstance(item, bytes):
                await ws.send(item)
            else:
                await ws.send(item)

    def _on_binary(self, frame: bytes) -> None:
        if frame[:1] == bytes([TTS_FRAME]) and len(frame) >= 5:
            (chunk_id,) = struct.unpack("<I", frame[1:5])
            self.play.enqueue(chunk_id, frame[5:])

    async def _on_json(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "stop_playback":
            self._suspended = False
            self.play.stop_all()
        elif kind == "resume_playback":
            # the Pi decided it was the room, not the operator
            self._suspended = False
            self.play.resume()
        elif kind == "wake":
            self.play.chime()
        elif kind == "shutdown":
            log.info("asked to shut down")
            raise SystemExit(0)
        elif kind == "ready":
            log.info("ready (wake word: %s)", ev.get("wake") or "off")
        elif kind == "transcript":
            log.info("heard: %s", ev.get("text", ""))
        elif kind == "assistant_text":
            log.info("jarvis[%s]: %s", ev.get("tier", "?"), ev.get("text", ""))
        elif kind == "state":
            log.debug("state: %s", ev.get("state"))
        elif kind == "clap":
            log.info("clap: %s", ev.get("result", ""))
        elif kind == "error":
            log.error("%s", ev.get("message"))


def _load_config(path: Path) -> dict:
    """Config file, then environment. The token belongs in a 0600 file rather
    than an argv the whole machine can read in `ps`."""
    cfg: dict = {}
    if path.exists():
        with contextlib.suppress(ValueError):
            cfg = json.loads(path.read_text())
    for key, env in (("url", "JARVIS_VOICE_URL"),
                     ("token", "JARVIS_VOICE_CLIENT_TOKEN"),
                     ("name", "JARVIS_VOICE_CLIENT_NAME"),
                     ("project", "JARVIS_VOICE_PROJECT")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="voicedesk", description="Jarvis desktop voice mode, headless.")
    ap.add_argument("--config", type=Path,
                    default=Path.home() / ".config" / "jarvis" / "voicedesk.json")
    ap.add_argument("--url", help="wss://<jarvis>/api/voice/ws")
    ap.add_argument("--token", help="prefer the config file — argv is world-readable")
    ap.add_argument("--name", help="what this machine is called in Jarvis")
    ap.add_argument("--project", help="pin this client's conversations to a project")
    ap.add_argument("--in-device", help="input device index or name")
    ap.add_argument("--out-device", help="output device index or name")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    if args.list_devices:
        from .audio import list_devices
        print(list_devices())
        return 0

    cfg = _load_config(args.config)
    url = args.url or cfg.get("url")
    token = args.token or cfg.get("token")
    if not url or not token:
        print(f"need a url and a token — put them in {args.config} as "
              f'{{"url": "wss://…/api/voice/ws", "token": "…"}}, or pass '
              f"--url/--token.", file=sys.stderr)
        return 2

    def device(v):
        if v is None:
            return None
        return int(v) if str(v).isdigit() else v

    desk = VoiceDesk(url, token,
                     name=args.name or cfg.get("name") or os.uname().nodename,
                     in_device=device(args.in_device or cfg.get("in_device")),
                     out_device=device(args.out_device or cfg.get("out_device")),
                     project=args.project or cfg.get("project"))
    try:
        asyncio.run(desk.run())
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
