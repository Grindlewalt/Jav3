"""The music server (TARMAC) settings and the in-page player's routes.

GUI-only: every route needs a logged-in operator on the same origin. The stream
route is fetched by an <audio> tag on this origin, so the session cookie rides
along with it automatically.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import gui, tarmac
from .auth import require_same_origin, require_user

router = APIRouter(prefix="/api/media", tags=["media"],
                   dependencies=[Depends(require_user), Depends(require_same_origin)])


class TarmacBody(BaseModel):
    url: str = ""


@router.get("/tarmac")
async def tarmac_get():
    return {"url": await tarmac.get_config()}


@router.put("/tarmac")
async def tarmac_put(body: TarmacBody):
    try:
        await tarmac.set_config(body.url)
    except tarmac.TarmacError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"url": await tarmac.get_config()}


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

    Range is forwarded in and the 206 passed straight back out. That is not
    optional polish: without Content-Range the <audio> element cannot seek, and
    Safari refuses to start the element at all.
    """
    try:
        handle = await tarmac.open_stream(track_id, request.headers.get("range"))
    except tarmac.TarmacError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return StreamingResponse(handle.chunks(), status_code=handle.status,
                             headers=handle.headers)


class PlayerStateBody(BaseModel):
    # which tab is reporting, so a second tab left playing from earlier cannot
    # be mistaken for this request having started
    tab: str = ""
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
        "started": body.started, "error": body.error, "tab": body.tab,
    })
    # count the play once, when a new track actually starts — /stream/:id does
    # not touch TARMAC's plays table, so nothing else would record it
    if body.started and body.track_id and prev.get("id") != body.track_id:
        await tarmac.scrobble(body.track_id)
    return state


@router.get("/tarmac/player")
async def tarmac_player():
    return gui.player_status()
