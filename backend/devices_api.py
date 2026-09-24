"""Devices: authorize a computer's CLI against the API with a pasted code.

Settings -> Add computer (an authenticated browser session) mints a one-time
code and shows `address=<host:port> code=<code>`; `jav3 login` on the other
machine posts the code to the address and receives a revocable device token
(`backend/devicetokens.py`, stored sha256). The session that minted the code is
the authorization, so there is no second approve step. Ticket lifecycle and the
threat notes live in `backend/pastelogin.py`.

Routers:
- `router`       operator side: cookie session + same-origin, /api/devices.
- `pair_router`  device side: redeem (NO auth — the device has no credential
                 yet; throttled), whoami and revoke-self (device bearer).
- `cli_router`   the CLI and its installer, as unauthenticated static files.
"""
import ipaddress
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import devicetokens, lan, pastelogin, security
from .auth import require_actor, require_same_origin, require_user
from .config import settings
from .db import get_db

router = APIRouter(prefix="/api/devices", tags=["devices"],
                   dependencies=[Depends(require_user), Depends(require_same_origin)])
pair_router = APIRouter(prefix="/api/devices", tags=["devices"])
cli_router = APIRouter(prefix="/cli", tags=["devices"])

CLI_DIR = Path(__file__).resolve().parent.parent / "clients" / "jav3cli"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_BAD_CODE = ("invalid or expired login code — generate a new one in "
             "Settings → Add computer")

# A Host header is a bracketed IPv6 literal or a DNS name / IPv4, plus an
# optional port. Anything else (spaces, '=', quotes, shell metacharacters) is
# refused rather than echoed into the login string or a shell script.
_HOST_RE = re.compile(
    r"(\[[0-9a-f:.]{2,45}\]|[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?)(?::(\d{1,5}))?")


def _peer(request: Request) -> str:
    """The TCP peer. Deliberately NOT X-Forwarded-For / CF-Connecting-IP: with
    nothing trusted in front of a LAN server those are attacker-chosen, and a
    forged value per request would be a fresh per-peer throttle budget."""
    return (getattr(request.client, "host", None) or "?")[:64]


def _throttled(request: Request) -> str:
    peer = _peer(request)
    try:
        pastelogin.throttle(peer)
    except pastelogin.TooMany as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    return peer


def _is_loopback(host: str) -> bool:
    h = host.strip("[]")
    if h in ("localhost", "0.0.0.0") or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _host_header(request: Request) -> tuple[str, str] | None:
    """(host, host[:port]) from a well-formed Host header, else None."""
    raw = (request.headers.get("host") or "").strip().lower()
    m = _HOST_RE.fullmatch(raw)
    if not m or (m.group(2) and not 0 < int(m.group(2)) < 65536):
        return None
    return m.group(1), raw


def _scheme(request: Request) -> str:
    return "https" if (request.url.scheme == "https" or settings.cookie_secure) else "http"


def _address(request: Request) -> str:
    """Where the other computer should post the code.

    The Host the operator's browser used is the one address proven to reach
    this server, so it wins — unless it is loopback (browsing on the box itself,
    or through an SSH tunnel), which another machine cannot use. Then the mDNS
    name if it is being advertised, else the first LAN IP, on the LAN port.
    http is left implicit; https is spelled out so the CLI never downgrades.
    """
    hh = _host_header(request)
    if hh and not _is_loopback(hh[0]):
        addr = hh[1]
    else:
        ips = lan.lan_ips()
        host = lan.advertised_hostname() or (ips[0] if ips else "") or "localhost"
        addr = f"{host}:{settings.lan_port}"
    return addr if _scheme(request) == "http" else f"https://{addr}"


# --- operator side (cookie session) -------------------------------------------

class CodeBody(BaseModel):
    name: str = Field("", max_length=64)


@router.post("/login-code")
async def mint_login_code(body: CodeBody, request: Request, response: Response,
                          user: dict = Depends(require_user)):
    """A pre-approved, single-use login code for `jav3 login`. The raw code is
    in this response and nowhere else on the server."""
    code, t = pastelogin.mint(body.name, by=user["username"])
    address = _address(request)
    response.headers.update(_NO_STORE)
    return {"login": f"address={address} code={code}", "address": address,
            "code": code, "name": t.name, "expires_at": t.expires,
            "ttl_seconds": pastelogin.TTL_SECONDS}


@router.get("")
async def list_devices():
    """Computers with live tokens (never the token itself)."""
    return {"devices": await devicetokens.list_tokens()}


@router.delete("/{token_id:int}")
async def revoke_device(token_id: int):
    if not await devicetokens.revoke(token_id):
        raise HTTPException(status_code=404, detail="no such device token")
    return {"ok": True}


# --- device side ----------------------------------------------------------------

class RedeemBody(BaseModel):
    code: str = Field(..., max_length=256)
    name: str = Field("", max_length=256)
    hostname: str = Field("", max_length=256)
    platform: str = Field("", max_length=256)


@pair_router.post("/login")
async def redeem_login_code(body: RedeemBody, request: Request, response: Response):
    """Trade a login code for a device token — once.

    Every failure is the same 401 with the same text, whether the code was
    malformed, never issued, expired or already used. `redeem` is synchronous
    and pops the ticket, so the spend happens before the first await below.
    """
    peer = _throttled(request)
    t = pastelogin.redeem(body.code)
    if t is None:
        pastelogin.note_wrong(peer)
        raise HTTPException(status_code=401, detail=_BAD_CODE)
    name = t.name or body.name.strip()[:64] or body.hostname.strip()[:64] or "cli"
    raw, tid = await devicetokens.mint(name, hostname=body.hostname,
                                       platform=body.platform, by=t.by)
    db = await get_db()
    try:
        await security.raise_event(
            db, kind="device_enrolled", severity="info",
            summary=f"computer '{name}' logged in with a code minted by "
                    f"{t.by or '?'} (from {peer})",
            detail={"device_id": tid, "name": name,
                    "hostname": body.hostname[:128], "platform": body.platform[:32],
                    "peer": peer, "minted_by": t.by, "minted_at": t.created,
                    "agent": request.headers.get("user-agent", "")[:120]})
    except Exception:  # noqa: BLE001 — the audit line must not eat the token
        pass
    finally:
        await db.close()
    response.headers.update(_NO_STORE)
    return {"ok": True, "token": raw, "device_id": tid, "name": name}


@pair_router.get("/whoami")
async def whoami(actor: dict = Depends(require_actor)):
    """Echo the authenticated actor — the operator cookie or a device token."""
    return actor


@pair_router.delete("/self")
async def revoke_self(actor: dict = Depends(require_actor)):
    """`jav3 logout`: a device revokes its own token. Only ever the presenting
    token — there is no id parameter to point at another one."""
    if not actor.get("is_device"):
        raise HTTPException(status_code=400, detail="only a device token can "
                            "revoke itself; use Settings → Devices")
    await devicetokens.revoke(actor["device_id"])
    return {"ok": True}


# --- the CLI, as static files ------------------------------------------------------

@cli_router.get("/jav3")
async def cli_file():
    return PlainTextResponse((CLI_DIR / "jav3").read_text(),
                             media_type="text/x-python")


@cli_router.get("/install.sh")
async def cli_installer(request: Request):
    """The CLI installer, pointed back at the address it was fetched from."""
    hh = _host_header(request)
    if hh is None:
        raise HTTPException(status_code=400, detail="bad Host header")
    script = (CLI_DIR / "install.sh").read_text().replace(
        "@@BASE@@", f"{_scheme(request)}://{hh[1]}")
    return PlainTextResponse(script, media_type="text/x-shellscript")
