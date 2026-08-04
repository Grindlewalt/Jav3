"""Voice mode transport: the browser-facing WebSocket plus a status probe.

The WS carries mixed frames (binary 0x01 mic PCM up / 0x02 TTS PCM down,
JSON control both ways — the shapes live in voice.py's docstring). Auth is
the session cookie, validated manually because a WebSocket can't use
Depends(require_user) — same pattern as guest_shell.py."""
import json
import logging

from fastapi import APIRouter, Depends, WebSocket

from . import voice
from .auth import COOKIE_NAME, require_user, user_from_token
from .config import settings

log = logging.getLogger(__name__)

router = APIRouter()

# one voice session at a time — it's a desktop mode for one operator; a
# second tab taking over mirrors the sidecar's own single-client rule
_current: voice.VoiceSession | None = None


@router.websocket("/api/voice/ws")
async def voice_ws(ws: WebSocket):
    global _current
    if not settings.voice_enabled:
        await ws.close(code=4404)
        return
    if user_from_token(ws.cookies.get(COOKIE_NAME)) is None:
        await ws.close(code=4401)
        return
    await ws.accept()

    if _current is not None:
        await _current.close()

    async def send_json(obj: dict) -> None:
        try:
            await ws.send_text(json.dumps(obj))
        except Exception:  # noqa: BLE001 — browser gone; rx loop ends us
            pass

    async def send_bytes(data: bytes) -> None:
        try:
            await ws.send_bytes(data)
        except Exception:  # noqa: BLE001
            pass

    session = voice.VoiceSession(send_json, send_bytes)
    _current = session
    await session.start()
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                await session.on_browser_bytes(data)
                continue
            try:
                await session.on_browser_json(json.loads(msg.get("text") or "{}"))
            except ValueError:
                await send_json({"type": "error", "message": "bad json"})
    except Exception:  # noqa: BLE001 — a dying socket is a normal ending
        pass
    finally:
        if _current is session:
            _current = None
        await session.close()


@router.get("/api/voice/status", dependencies=[Depends(require_user)])
async def voice_status():
    s = _current
    return {
        "enabled": settings.voice_enabled,
        "session": None if s is None else {
            "state": s.state,
            "force_tier": s.force_tier,
            "conversation_id": s.cid,
            "sidecar_connected": s.link.connected,
            "queued": len(s.queued),
            "workers": [{"conversation_id": cid, "task": w["task"]}
                        for cid, w in s.workers.items()],
        },
    }
