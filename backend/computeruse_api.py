"""Computer use: the operator's control surface, and the client's socket.

Two audiences here. The /api/computeruse/* routes are the GUI's — they need a
logged-in operator, and they are the only way a folder grant comes into
existence. The /api/computeruse/agent WebSocket is the desktop client's; it
authenticates with the pairing token instead of a session cookie, because it is
a daemon on the operator's machine rather than a browser.

Note what is deliberately absent: there is no route that takes a verb from the
GUI and forwards it verbatim, and none that adds a grant on the agent's behalf.
"""
from __future__ import annotations

import asyncio
import io
import json
import time
import uuid
import zipfile

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import computeruse as cu, security
from .auth import require_user
from .config import settings
from .db import get_db

router = APIRouter(prefix="/api/computeruse", tags=["computeruse"],
                   dependencies=[Depends(require_user)])

# the socket authenticates itself, so it must not sit behind require_user
ws_router = APIRouter(prefix="/api/computeruse", tags=["computeruse"])


class GrantBody(BaseModel):
    root: str
    label: str = ""


class TokenBody(BaseModel):
    rotate: bool = False


class JellyfinBody(BaseModel):
    url: str = ""
    key: str = ""      # blank leaves the stored key alone


@router.get("/status")
async def status():
    """Everything the tab renders: who is connected, what they can reach, and
    which folders are granted."""
    return {
        "clients": [c.describe() for c in cu.clients()],
        "grants": [{"id": g.id, "root": g.root, "label": g.label}
                   for g in await cu.list_grants()],
        "verbs": {name: spec["doc"] for name, spec in cu.VERBS.items()},
    }


@router.get("/token")
async def token():
    return {"token": await cu.pairing_token()}


@router.post("/token")
async def rotate_token(body: TokenBody):
    return {"token": await cu.pairing_token(rotate=body.rotate)}


@router.post("/grants")
async def create_grant(body: GrantBody):
    try:
        g = await cu.add_grant(body.root, body.label)
    except cu.VerbError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": g.id, "root": g.root, "label": g.label}


@router.delete("/grants/{grant_id}")
async def delete_grant(grant_id: int):
    await cu.remove_grant(grant_id)
    return {"ok": True}


@router.get("/jellyfin")
async def jellyfin_get():
    url, key = await cu.jellyfin_config()
    # the key never leaves the host — the tab only learns whether one is set
    return {"url": url, "key_set": bool(key)}


@router.put("/jellyfin")
async def jellyfin_put(body: JellyfinBody):
    try:
        await cu.set_jellyfin_config(body.url, body.key)
    except cu.VerbError as e:
        raise HTTPException(status_code=400, detail=str(e))
    url, key = await cu.jellyfin_config()
    return {"url": url, "key_set": bool(key)}


@router.get("/client.zip")
async def client_zip():
    """The client, zipped, so a machine that has never seen this repo can get it.

    Source only — the .py files, requirements and README. No config and no
    token: the operator supplies those on the command line, and a download
    anyone with a session could fetch must not carry a credential.
    """
    src = settings.base_dir / "clients" / "computeruse"
    if not src.is_dir():
        raise HTTPException(status_code=500, detail="client source is missing")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.iterdir()):
            if f.is_file() and f.suffix in (".py", ".txt", ".md"):
                z.write(f, arcname=f"computeruse/{f.name}")
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="computeruse.zip"'})


@router.post("/probe")
async def probe(client_id: str | None = None):
    """Ask a client to describe itself. The one operator-triggered verb — it
    reads state and changes nothing, so the tab can show live screens and sinks
    without the operator having to ask Jarvis."""
    try:
        return await cu.dispatch("status", {}, client_id)
    except cu.VerbError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Rejected pairing attempts, per peer. Deduped into one security event per
# burst so a scanner hammering the endpoint raises one alert rather than
# thousands, which would bury everything else in the Review Center.
_bad_attempts: dict[str, list] = {}
_BURST_WINDOW = 300.0


async def _note_bad_token(peer: str) -> None:
    now = time.time()
    count, first, alerted = _bad_attempts.get(peer, [0, now, False])
    if now - first > _BURST_WINDOW:
        count, first, alerted = 0, now, False
    count += 1
    should_alert = not alerted and count >= 3
    _bad_attempts[peer] = [count, first, alerted or should_alert]
    if not should_alert:
        return
    db = await get_db()
    try:
        await security.raise_event(
            db, kind="computeruse_auth",
            severity="warn",
            summary=f"{count} rejected computer-use pairing attempts from {peer}",
            detail={"peer": peer, "attempts": count,
                    "note": "the agent WebSocket takes a pairing token instead "
                            "of a session cookie; repeated failures mean "
                            "something is probing it"})
    except Exception:
        pass
    finally:
        await db.close()


@ws_router.websocket("/agent")
async def agent_socket(ws: WebSocket):
    """A desktop client's connection.

    The client dials in, presents the pairing token, and then does nothing but
    answer verbs. It never sends commands to us and we never send it anything
    that is not a validated verb from cu.VERBS.

    This is the one route in the app with no session cookie behind it — a daemon
    has no browser to log in with. So it is also the one route where a failed
    auth is worth recording: if Jarvis is published, this endpoint is reachable
    by anyone who gets past whatever fronts it, and a run of rejected tokens is
    something the operator should be able to see.
    """
    await ws.accept()
    client = None
    try:
        hello = json.loads(await ws.receive_text())
        if not await cu.check_token(hello.get("token", "")):
            peer = getattr(ws.client, "host", "?")
            await _note_bad_token(peer)
            # a small delay costs a legitimate client nothing and makes the
            # endpoint useless for guessing at volume
            await asyncio.sleep(1.0)
            await ws.send_text(json.dumps({"ok": False, "error": "bad pairing token"}))
            await ws.close(code=4401)
            return
        name = str(hello.get("name") or "desktop")[:60]
        client = cu.Client(
            id=f"{name}-{uuid.uuid4().hex[:6]}",
            name=name,
            platform=str(hello.get("platform") or "?")[:20],
            caps=hello.get("caps") if isinstance(hello.get("caps"), dict) else {},
            send=ws.send_text,
        )
        cu.register(client)
        await ws.send_text(json.dumps({
            "ok": True, "client_id": client.id,
            "grants": [g.root for g in await cu.list_grants()],
        }))
        while True:
            msg = json.loads(await ws.receive_text())
            # replies only: a client has no way to ask us for anything
            if msg.get("id"):
                cu.resolve_result(client.id, msg["id"], msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close(code=1011)
        except Exception:
            pass
    finally:
        if client is not None:
            cu.unregister(client.id)
