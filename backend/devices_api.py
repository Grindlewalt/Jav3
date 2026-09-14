"""Device-authorization enrollment: authorize a new device/CLI against Jarvis's
API by confirming it in a logged-in browser, instead of pasting a raw key into a
terminal. Same RFC 8628 shape as the computer-use pairing (`backend/pairing.py`),
but generic: on the operator's confirm, the approved poll mints a per-device,
revocable API token (`backend/devicetokens.py`).

Two routers:
- `router`  (operator side)  — behind `require_user`, under /api/devices.
- `pair_router` (device side) — NO auth (the device has no credential yet),
  throttled; needs a Cloudflare Access Bypass policy on /api/devices/pair/*,
  exactly like /api/computeruse/pair/* (see pairing.py's module docstring).
"""
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import devicetokens, pairing, security
from .auth import require_actor, require_same_origin, require_user
from .db import get_db

KIND = "device"

router = APIRouter(prefix="/api/devices", tags=["devices"],
                   dependencies=[Depends(require_user), Depends(require_same_origin)])
pair_router = APIRouter(prefix="/api/devices", tags=["devices"])


def _peer(request: Request) -> str:
    for h in ("cf-connecting-ip", "x-forwarded-for"):
        v = request.headers.get(h)
        if v:
            return v.split(",")[0].strip()[:64]
    return getattr(request.client, "host", "?") or "?"


def _throttled(request: Request) -> str:
    peer = _peer(request)
    try:
        pairing.throttle(peer, kind="device")
    except pairing.TooMany as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    return peer


def _ticket_view(t: pairing.Ticket) -> dict:
    d = t.public()
    d["confirm_path"] = f"/pair/{t.code}"
    return d


# --- operator side (require_user) ---------------------------------------------

class EnrollBody(BaseModel):
    name: str = ""


@router.post("/enroll")
async def enroll_create(body: EnrollBody):
    """A fresh enrollment code for a device the operator is about to authorize."""
    return _ticket_view(pairing.create(body.name, kind=KIND))


@router.get("/enroll")
async def enroll_list():
    return {"tickets": [_ticket_view(t) for t in pairing.live(kind=KIND)],
            "ttl_seconds": pairing.TTL_SECONDS}


@router.get("/enroll/{code}")
async def enroll_status(code: str):
    t = pairing.get(code, kind=KIND)
    if t is None:
        raise HTTPException(status_code=404, detail=pairing.Unknown().args[0])
    return _ticket_view(t)


@router.post("/enroll/{code}/approve")
async def enroll_approve(code: str, request: Request,
                         user: dict = Depends(require_user)):
    """The operator's yes. The moment a device token is committed to being
    minted, so it is the moment that gets recorded."""
    try:
        t = pairing.approve(code, by=user["username"], kind=KIND)
    except pairing.PairingError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    db = await get_db()
    try:
        await security.raise_event(
            db, kind="device_enrolled", severity="info",
            summary=f"{user['username']} authorized device "
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
    try:
        t = pairing.deny(code, kind=KIND)
    except pairing.PairingError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    return _ticket_view(t)


@router.get("")
async def list_devices():
    """Enrolled devices with live tokens (never the token itself)."""
    return {"devices": await devicetokens.list_tokens()}


@router.delete("/{token_id}")
async def revoke_device(token_id: int):
    if not await devicetokens.revoke(token_id):
        raise HTTPException(status_code=404, detail="no such device token")
    return {"ok": True}


# --- device side (unauthenticated, throttled) ---------------------------------

class ClaimBody(BaseModel):
    code: str
    name: str = ""
    hostname: str = ""
    platform: str = ""


class PollBody(BaseModel):
    code: str
    device_secret: str


@pair_router.post("/pair/claim")
async def pair_claim(body: ClaimBody, request: Request):
    peer = _throttled(request)
    try:
        t = pairing.claim(body.code, name=body.name, hostname=body.hostname,
                          platform=body.platform, peer=peer, kind=KIND,
                          agent=request.headers.get("user-agent", ""))
    except pairing.Unknown as e:
        pairing.note_wrong_code(peer, kind="device")
        raise HTTPException(status_code=e.status, detail=str(e))
    except pairing.PairingError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    return {"ok": True, "code": t.code, "name": t.name,
            "device_secret": t.device_secret,
            "confirm_path": f"/pair/{t.code}",
            "interval": pairing.POLL_INTERVAL,
            "expires_in": max(0, int(t.expires - time.time()))}


@pair_router.post("/pair/poll")
async def pair_poll(body: PollBody, request: Request):
    """Pending, denied, or — once — the minted API token.

    The ticket is spent SYNCHRONOUSLY (release before any await), so two
    concurrent polls of one approved code cannot both mint a token: release
    clears the device secret, so the second poll's pairing.poll() raises Unknown
    (404). Release-before-mint is fail-closed: if minting then fails the code is
    spent and the device must re-enroll — never a double-mint.
    """
    peer = _throttled(request)
    try:
        t = pairing.poll(body.code, body.device_secret, kind=KIND)
    except pairing.Unknown as e:
        pairing.note_wrong_code(peer, kind="device")
        raise HTTPException(status_code=e.status, detail=str(e))
    if t.state != "approved":
        return {"ok": True, "state": t.state, "interval": pairing.POLL_INTERVAL,
                "expires_in": max(0, int(t.expires - time.time()))}
    # capture what the token needs, then spend the ticket with no await in between
    name, claim, by = t.name, dict(t.claim), t.approved_by
    pairing.release(t)
    raw, _tid = await devicetokens.mint(
        name, hostname=claim.get("hostname", ""),
        platform=claim.get("platform", ""), by=by)
    return {"ok": True, "state": "approved", "name": name, "token": raw}


@pair_router.get("/whoami")
async def whoami(actor: dict = Depends(require_actor)):
    """Echo the authenticated actor — works with either the operator cookie or a
    device Bearer token, so a freshly paired device can prove its token works."""
    return actor
