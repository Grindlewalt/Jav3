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

from . import cfaccess, computeruse as cu, gui, pairing, security, tarmac
from .auth import COOKIE_NAME, require_same_origin, require_user, user_from_token
from .config import settings
from .db import get_db

router = APIRouter(prefix="/api/computeruse", tags=["computeruse"],
                   dependencies=[Depends(require_user), Depends(require_same_origin)])

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
        # what this host would hand out right now, so the tab can say "that
        # machine is running an older download" instead of leaving a stale
        # client looking like a broken one
        "served_version": cu.served_build_id(),
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


class CFAccessBody(BaseModel):
    client_id: str = ""
    secret: str = ""        # blank keeps the stored one
    hosts: list[str] = []


@router.get("/cfaccess")
async def cfaccess_get():
    """The stored Access service token, for a logged-in operator — the id, the
    bound hosts, and the secret ONLY while the reveal window is open.

    It used to return the secret unconditionally, so the set-up command could
    carry it, and the docstring here defended that as the same exposure as the
    pairing token. It was worse: the pairing token opens the computer-use
    socket, the Access secret opens the front door of everything behind
    Cloudflare. Pairing now gets both onto a new machine without the browser
    holding either, so the secret leaves the host only inside the ten-minute
    window the Settings page opens on purpose (see cfaccess.reveal).
    """
    cid, sec = cfaccess.get()
    until = cfaccess.revealed_until()
    return {"client_id": cid, "secret": sec if until else "",
            "configured": bool(cid and sec), "hosts": cfaccess.hosts(),
            "revealed_until": until}


class RevealBody(BaseModel):
    confirm: str = ""


@router.post("/cfaccess/reveal")
async def cfaccess_reveal(body: RevealBody, request: Request,
                          user: dict = Depends(require_user)):
    """Open the reveal window. Testing only, and the GUI says so.

    The typed word is checked here as well as in the browser, because the
    ceremony is the control: a request that skips the page must not skip it.
    Every opening is a security event, so the Review Center holds the record of
    when the secret was made readable and by whom.
    """
    if (body.confirm or "").strip().lower() != cfaccess.REVEAL_WORD:
        raise HTTPException(status_code=400,
                            detail=f"type {cfaccess.REVEAL_WORD!r} to confirm")
    if not cfaccess.configured():
        raise HTTPException(status_code=400, detail="no Access token is stored")
    until = cfaccess.reveal()
    db = await get_db()
    try:
        await security.raise_event(
            db, kind="cfaccess_revealed", severity="warn",
            summary=f"Cloudflare Access secret made readable in the GUI by "
                    f"{user['username']} for {cfaccess.REVEAL_SECONDS // 60} min",
            detail={"by": user["username"], "until": until,
                    "peer": _peer(request),
                    "note": "the Settings page danger zone. The secret is in "
                            "the set-up command for that window; treat any "
                            "machine it was pasted on as holding it."})
    finally:
        await db.close()
    return {"ok": True, "revealed_until": until}


@router.post("/cfaccess/hide")
async def cfaccess_hide():
    cfaccess.hide()
    return {"ok": True, "revealed_until": None}


@router.put("/cfaccess")
async def cfaccess_put(body: CFAccessBody, request: Request):
    """Save a rotated token and get it onto every machine that is reachable.

    The reply names the machines that took it, because the ones it does not name
    are the operator's remaining work — a client that is offline right now
    cannot be told anything, since Jarvis is behind the very thing being
    rotated.
    """
    # Bind to the hostname this request arrived on. That is Jarvis's own public
    # name, which the host cannot otherwise know and which is by definition one
    # of the places this token is presented. Without it the token ends up bound
    # only to the music server, which is the one host it was migrated from.
    here = (request.url.hostname or "").lower()
    bound = sorted(set(body.hosts or cfaccess.hosts()) | ({here} if here else set()))
    try:
        cfaccess.set_token(body.client_id, body.secret, bound)
    except cfaccess.CFAccessError as e:
        raise HTTPException(status_code=400, detail=str(e))
    updated = await cu.broadcast_access_token()
    connected = [c.name for c in cu.clients()]
    return {"ok": True, "configured": cfaccess.configured(),
            "hosts": cfaccess.hosts(), "updated": updated,
            "missed": [n for n in connected if n not in updated]}


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


def _peer(request: Request) -> str:
    """Who a request came from, for the record and for throttling hints.
    Forgeable by anything that can reach the app directly, so it is a hint
    about who and never an input to a security decision (see auth._peer)."""
    for h in ("cf-connecting-ip", "x-forwarded-for"):
        v = request.headers.get(h)
        if v:
            return v.split(",")[0].strip()[:64]
    return getattr(request.client, "host", "?") or "?"


# --- pairing: the operator's side ----------------------------------------------
#
# These sit behind require_user like the rest of the router. The machine's side
# is further down, on ws_router, under /pair/ — a different prefix on purpose,
# so that the one Cloudflare Access Bypass policy this needs can be scoped to
# exactly the routes built to be reachable without a credential.

class PairCreateBody(BaseModel):
    name: str = ""


def _ticket_view(t: pairing.Ticket) -> dict:
    d = t.public()
    d["confirm_path"] = f"/pair/{t.code}"
    d["download_path"] = f"/api/computeruse/pair/client.tar.gz?code={t.code}"
    return d


@router.post("/enroll")
async def enroll_create(body: PairCreateBody):
    """A fresh pairing code for a machine about to be set up."""
    return _ticket_view(pairing.create(body.name, kind="computeruse"))


@router.get("/enroll")
async def enroll_list():
    return {"tickets": [_ticket_view(t) for t in pairing.live(kind="computeruse")],
            "ttl_seconds": pairing.TTL_SECONDS}


@router.get("/enroll/{code}")
async def enroll_status(code: str):
    """What the confirm page and the wizard both poll: has a machine claimed
    it yet, and which one."""
    t = pairing.get(code, kind="computeruse")
    if t is None:
        raise HTTPException(status_code=404, detail=pairing.Unknown().args[0])
    return _ticket_view(t)


@router.post("/enroll/{code}/approve")
async def enroll_approve(code: str, request: Request,
                         user: dict = Depends(require_user)):
    """The operator's yes. This is the moment the credentials are committed to
    leaving the host, so it is the moment that gets recorded."""
    try:
        t = pairing.approve(code, by=user["username"], kind="computeruse")
    except pairing.PairingError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    db = await get_db()
    try:
        await security.raise_event(
            db, kind="machine_paired", severity="info",
            summary=f"{user['username']} paired machine "
                    f"'{t.claim.get('name') or t.name or '?'}' "
                    f"({t.claim.get('hostname') or '?'}, "
                    f"{t.claim.get('platform') or '?'}) with code {t.code}",
            detail={"code": t.code, "claim": t.claim, "contested": t.contested,
                    "by": user["username"], "peer": _peer(request)})
    finally:
        await db.close()
    return _ticket_view(t)


@router.post("/enroll/{code}/deny")
async def enroll_deny(code: str):
    """Also how a code is cancelled before anything has claimed it."""
    try:
        t = pairing.deny(code, kind="computeruse")
    except pairing.PairingError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    return _ticket_view(t)


# --- pairing: the machine's side -----------------------------------------------
#
# No session, no pairing token: the whole point is that the machine has nothing
# yet. Behind Cloudflare Access this prefix needs a Bypass policy, which makes
# these reachable by anyone, and they are written for that: throttled, no
# credential without an approved ticket and its device secret, and an unknown
# code indistinguishable from an expired one.

class ClaimBody(BaseModel):
    code: str
    name: str = ""
    hostname: str = ""
    platform: str = ""


class PollBody(BaseModel):
    code: str
    device_secret: str


def _throttled(request: Request) -> str:
    peer = _peer(request)
    try:
        pairing.throttle(peer, kind="computeruse")
    except pairing.TooMany as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    return peer


@ws_router.post("/pair/claim")
async def pair_claim(body: ClaimBody, request: Request):
    peer = _throttled(request)
    try:
        t = pairing.claim(body.code, name=body.name, hostname=body.hostname,
                          platform=body.platform, peer=peer, kind="computeruse",
                          agent=request.headers.get("user-agent", ""))
    except pairing.Unknown as e:
        pairing.note_wrong_code(peer, kind="computeruse")
        raise HTTPException(status_code=e.status, detail=str(e))
    except pairing.PairingError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    return {"ok": True, "code": t.code, "name": t.name,
            "device_secret": t.device_secret,
            "confirm_path": f"/pair/{t.code}",
            "interval": pairing.POLL_INTERVAL,
            "expires_in": max(0, int(t.expires - time.time()))}


@ws_router.post("/pair/poll")
async def pair_poll(body: PollBody, request: Request):
    """Pending, denied, or — once — the credentials.

    The reply that carries them is the only place the pairing token and the
    Access service token ever leave the host for a machine, and release() spends
    the ticket in the same breath, so a replayed poll gets "released" and
    nothing else.
    """
    peer = _throttled(request)
    try:
        t = pairing.poll(body.code, body.device_secret, kind="computeruse")
    except pairing.Unknown as e:
        pairing.note_wrong_code(peer, kind="computeruse")
        raise HTTPException(status_code=e.status, detail=str(e))
    if t.state != "approved":
        return {"ok": True, "state": t.state, "interval": pairing.POLL_INTERVAL,
                "expires_in": max(0, int(t.expires - time.time()))}
    # spend the ticket synchronously BEFORE any await, so a concurrent poll can't
    # be handed the credentials twice, and a `deny` landing during the await
    # can't be clobbered by an unconditional release afterwards
    name = t.name
    pairing.release(t)
    cid, sec = cfaccess.get()
    return {"ok": True, "state": "approved", "name": name,
            "token": await cu.pairing_token(),
            "cf_access_id": cid, "cf_access_secret": sec}


@ws_router.get("/pair/client.tar.gz")
async def pair_client_tar(request: Request, code: str = ""):
    """The client source, for a machine holding a live pairing code.

    Source only, exactly as /client.tar.gz — but let through by the code rather
    than the pairing token, since the machine does not have that yet. A wrong
    code costs guessing budget like everywhere else on this prefix.
    """
    peer = _throttled(request)
    if pairing.get(code, kind="computeruse") is None:
        pairing.note_wrong_code(peer, kind="computeruse")
        raise HTTPException(status_code=401,
                            detail=pairing.Unknown().args[0])
    return StreamingResponse(_tar_bytes(), media_type="application/gzip",
                             headers={**_TAR_DISPOSITION, **_NO_CACHE})


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


# The client download must never be cached, and saying so is not optional.
#
# A CDN in front of Jarvis will cache these by FILE EXTENSION without being
# asked: ".gz" and ".zip" are both on Cloudflare's default list, and it applied
# a four-hour TTL of its own to a response the origin said nothing about. The
# effect was that every fix shipped to the client was invisible to anyone
# downloading through the public hostname — set-up kept installing a stale
# build, including one that predated the fix for the very error being chased.
# Observed as cf-cache-status: HIT with an age of 46 minutes on a file that had
# been rebuilt minutes earlier.
#
# no-store is the one directive that keeps it out of both the edge cache and the
# browser's. CDN-Cache-Control says the same thing again to the CDN
# specifically, since that is the header a CDN prefers when it is present.
_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "CDN-Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def _client_source() -> list[Path]:
    """The files the client is made of. Source only — the config file is what
    would carry a token, and it is not here."""
    src = settings.base_dir / "clients" / "computeruse"
    if not src.is_dir():
        raise HTTPException(status_code=500, detail="client source is missing")
    return [f for f in sorted(src.iterdir())
            if f.is_file() and f.suffix in (".py", ".txt", ".md")]


def _tar_bytes() -> io.BytesIO:
    """The client source as a gzipped tarball. Each route that serves it adds
    _NO_CACHE itself, in its own body, because a test reads the route's source
    for exactly that name — the CDN pinning an old build was found the hard way
    and the check is meant to stay visible at the point of serving."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for f in _client_source():
            t.add(f, arcname=f"computeruse/{f.name}")
    buf.seek(0)
    return buf


_TAR_DISPOSITION = {"Content-Disposition": 'attachment; filename="computeruse.tar.gz"'}


@ws_router.get("/client.tar.gz")
async def client_tar(request: Request, token: str | None = None):
    """The client as a tarball — what the set-up command actually fetches.

    `unzip` is not part of a base Linux install. The zip below downloaded fine
    and then died on `unzip: command not found`, leaving the operator with a
    half-finished set-up and no client. tar is in every base install, and on
    macOS too, so this is the one that is always openable.
    """
    await _download_auth(request, token)
    return StreamingResponse(_tar_bytes(), media_type="application/gzip",
                             headers={**_TAR_DISPOSITION, **_NO_CACHE})


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
        headers={"Content-Disposition": 'attachment; filename="computeruse.zip"',
                 **_NO_CACHE})


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
