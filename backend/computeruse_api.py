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
import tarfile
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import (APIRouter, Depends, HTTPException, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import computeruse as cu, gui, security, tarmac
from .auth import COOKIE_NAME, require_user, user_from_token
from .config import settings
from .db import get_db

router = APIRouter(prefix="/api/computeruse", tags=["computeruse"],
                   dependencies=[Depends(require_user)])

# the socket authenticates itself, so it must not sit behind require_user
ws_router = APIRouter(prefix="/api/computeruse", tags=["computeruse"])


class GrantBody(BaseModel):
    root: str
    label: str = ""
    client: str = ""       # "" means every machine


class PrivilegeBody(BaseModel):
    client: str
    capability: str
    allowed: bool


class TokenBody(BaseModel):
    rotate: bool = False


class JellyfinBody(BaseModel):
    url: str = ""
    key: str = ""      # blank leaves the stored key alone


class TarmacBody(BaseModel):
    url: str = ""
    cf_id: str = ""
    cf_secret: str = ""    # blank leaves the stored pair alone


@router.get("/status")
async def status():
    """Everything the tab renders: who is connected, what they can reach, and
    which folders are granted."""
    machines = []
    for c in cu.clients():
        machines.append({**c.describe(),
                         "privileges": await cu.privileges(c.name),
                         "grants": [{"id": g.id, "root": g.root, "label": g.label,
                                     "client": g.client}
                                    for g in await cu.list_grants(client=c.name)]})
    return {
        "clients": machines,
        "capabilities": cu.CAPABILITIES,
        "grants": [{"id": g.id, "root": g.root, "label": g.label,
                    "client": g.client} for g in await cu.list_grants()],
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
        g = await cu.add_grant(body.root, body.label, body.client)
    except cu.VerbError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Live, not on the next reconnect. This used to return restart_needed and
    # mean it: the client read its folder list once at connect, so a folder
    # added here did nothing until the operator went back to their laptop and
    # re-ran the set-up command.
    await cu.broadcast_grants()
    return {"id": g.id, "root": g.root, "label": g.label, "client": g.client,
            "restart_needed": False}


@router.put("/privileges")
async def set_privilege(body: PrivilegeBody):
    try:
        await cu.set_privilege(body.client, body.capability, body.allowed)
    except cu.VerbError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "privileges": await cu.privileges(body.client)}


@router.delete("/grants/{grant_id}")
async def delete_grant(grant_id: int):
    await cu.remove_grant(grant_id)
    # revoking has to reach the machine at once — a folder the operator just
    # took away must not stay readable until the client happens to restart
    await cu.broadcast_grants()
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


@router.get("/tarmac")
async def tarmac_get():
    url, cf_id, cf_secret = await tarmac.get_config()
    # the secret never leaves the host; the tab learns only that one is set
    return {"url": url, "cf_id": cf_id, "secret_set": bool(cf_secret)}


@router.put("/tarmac")
async def tarmac_put(body: TarmacBody):
    try:
        await tarmac.set_config(body.url, body.cf_id, body.cf_secret)
    except tarmac.TarmacError as e:
        raise HTTPException(status_code=400, detail=str(e))
    url, cf_id, cf_secret = await tarmac.get_config()
    return {"url": url, "cf_id": cf_id, "secret_set": bool(cf_secret)}


@router.post("/tarmac/test")
async def tarmac_test():
    """Ask the music server for its status, so the operator finds out here
    rather than by watching a chat turn fail."""
    try:
        return {"ok": True, "status": await tarmac.status()}
    except tarmac.TarmacError as e:
        return {"ok": False, "error": str(e)}


@router.get("/tarmac/stream/{track_id}")
async def tarmac_stream(track_id: int, request: Request):
    """Re-serve a library track on Jarvis's own origin.

    Route A of the player decision. TARMAC is a separate Cloudflare Access
    application, so the browser's Jarvis session buys it nothing there; the host
    holds the service token and proxies the bytes.

    Range is forwarded in and the 206 passed straight back out. That is not
    optional polish: without Content-Range the <audio> element cannot seek, and
    Safari refuses to start the element at all.

    Behind the router's require_user, which is correct here and NOT the trap
    client.zip fell into — this is fetched by an <audio> tag on the same origin,
    so the session cookie rides along automatically.
    """
    try:
        handle = await tarmac.open_stream(track_id, request.headers.get("range"))
    except tarmac.TarmacError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return StreamingResponse(handle.chunks(), status_code=handle.status,
                             headers=handle.headers)


class PlayerStateBody(BaseModel):
    track_id: int | None = None
    title: str = ""
    artist: str = ""
    paused: bool = True
    position: float = 0
    duration: float | None = None
    queue: int = 0
    volume: int = 100
    started: bool = False
    error: str = ""


@router.post("/tarmac/player/state")
async def tarmac_player_state(body: PlayerStateBody):
    """The in-page player reporting what it is really doing.

    The host cannot see an <audio> element, so without this every claim about
    playback would be a guess. `started` in particular only goes true once the
    tab's play() promise resolves — the difference between "accepted" and
    "audible" that the operator hit as silence.
    """
    prev = gui.player_status().get("track") or {}
    track = ({"id": body.track_id, "title": body.title, "artist": body.artist}
             if body.track_id else None)
    state = gui.player_report({
        "track": track, "paused": body.paused, "position": body.position,
        "duration": body.duration, "queue": body.queue, "volume": body.volume,
        "started": body.started, "error": body.error,
    })
    # count the play once, when a new track actually starts — /stream/:id does
    # not touch TARMAC's plays table, so nothing else would record it
    if body.started and body.track_id and prev.get("id") != body.track_id:
        await tarmac.scrobble(body.track_id)
    return state


@router.get("/tarmac/player")
async def tarmac_player():
    return gui.player_status()


async def _download_auth(request: Request, token: str | None) -> None:
    """Let the download through for a logged-in session OR the pairing token.

    NOT behind the session dependency: it is fetched by curl from a terminal on
    the machine being set up, which has no browser session. It sat behind
    require_user at first, so every download 401'd — and because curl had
    already created the output file, what the operator was left with was a
    zero-byte c.zip and "end of central directory signature not found".

    The pairing token authenticates it instead, by header or query. That token is
    in the set-up command anyway, and this only ever returns source: the .py
    files, requirements and README. No config, so no credential.
    """
    presented = (request.headers.get("x-jarvis-token") or token or "")
    if user_from_token(request.cookies.get(COOKIE_NAME)) is None:
        if not await cu.check_token(presented):
            raise HTTPException(
                status_code=401,
                detail="pass the pairing token as X-Jarvis-Token (the Computer "
                       "use tab builds the command for you)")


def _client_source() -> list[Path]:
    """The files the client is made of. Source only — the config file is what
    would carry a token, and it is not here."""
    src = settings.base_dir / "clients" / "computeruse"
    if not src.is_dir():
        raise HTTPException(status_code=500, detail="client source is missing")
    return [f for f in sorted(src.iterdir())
            if f.is_file() and f.suffix in (".py", ".txt", ".md")]


@ws_router.get("/client.tar.gz")
async def client_tar(request: Request, token: str | None = None):
    """The client as a tarball — what the set-up command actually fetches.

    `unzip` is not part of a base Linux install. The zip below downloaded fine
    and then died on `unzip: command not found`, leaving the operator with a
    half-finished set-up and no client. tar is in every base install, and on
    macOS too, so this is the one that is always openable.
    """
    await _download_auth(request, token)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for f in _client_source():
            t.add(f, arcname=f"computeruse/{f.name}")
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/gzip",
        headers={"Content-Disposition":
                 'attachment; filename="computeruse.tar.gz"'})


@ws_router.get("/client.zip")
async def client_zip(request: Request, token: str | None = None):
    """The same client, zipped, for a browser download or a machine with unzip."""
    await _download_auth(request, token)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in _client_source():
            z.write(f, arcname=f"computeruse/{f.name}")
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="computeruse.zip"'})


@ws_router.get("/ping")
async def ping(request: Request, token: str | None = None):
    """An ordinary HTTP hello, for the client's set-up step.

    Same auth as the download, and for the same reason: it is called by a daemon
    with no browser session. It exists because a wrong address, a missing
    Cloudflare service token and a rotated pairing token are indistinguishable
    from inside the WebSocket retry loop — all three come out as "server
    rejected the connection", forever. Over plain HTTP each has its own status
    code and the client can say which one it was.

    The names of connected machines are no more than the pairing token already
    reaches (it can drive all of them), and they let the client warn about a
    name it is about to collide with.
    """
    await _download_auth(request, token)
    return {"ok": True, "app": "jarvis",
            "connected": [c.name for c in cu.clients()]}


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
            # only this machine's folders: a path on the Mac is meaningless on
            # the Linux box, and sending it just gives that client a root it can
            # never resolve. cu.push_grants sends the same list on every later
            # change, so this is the first of many rather than the only one.
            "grants": [g.root for g in await cu.list_grants(client=client.name)],
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
