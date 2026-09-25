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
import json
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from . import devicetokens, lan, pastelogin, security
from .auth import require_any_actor, require_user
from .config import settings
from .db import get_db

router = APIRouter(prefix="/api/devices", tags=["devices"],
                   dependencies=[Depends(require_user)])
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
    r"(\[[0-9a-f:.]{2,45}\]|[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?)(?::([1-9]\d{0,4}))?")
# A final label that is numeric (or 0x-hex) makes the name an IPv4 literal to
# a resolver — `127.1`, `2130706433`, `0x7f000001` are all 127.0.0.1 — so it
# is accepted only in canonical dotted-quad form, which _is_loopback can judge.
_NUMERIC_LABEL = re.compile(r"(?:\d+|0x[0-9a-f]*)")


def _peer(request: Request) -> str:
    """The TCP peer. Deliberately NOT X-Forwarded-For / CF-Connecting-IP: with
    nothing trusted in front of a LAN server those are attacker-chosen, and a
    forged value per request would be a fresh per-peer throttle budget."""
    return (getattr(request.client, "host", None) or "?")[:64]


def _charge_miss(peer: str) -> None:
    """A code that did not redeem: 429 once the miss budget is spent, else
    charge it. Only ever reached on a miss — a valid code never sees this."""
    try:
        pastelogin.throttle(peer)
    except pastelogin.TooMany as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    pastelogin.note_wrong(peer)


def _is_loopback(host: str) -> bool:
    h = host.strip("[]")
    if h in ("localhost",) or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or ip.is_unspecified or bool(mapped and mapped.is_loopback)


def _host_header(request: Request) -> tuple[str, str] | None:
    """(host, host[:port]) from a well-formed Host header, else None."""
    raw = (request.headers.get("host") or "").strip().lower()
    m = _HOST_RE.fullmatch(raw)
    if not m or (m.group(2) and not 0 < int(m.group(2)) < 65536):
        return None
    host = m.group(1)
    if not host.startswith("["):
        if ".." in host:
            return None
        if _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
            try:
                if str(ipaddress.IPv4Address(host)) != host:
                    return None
            except ValueError:
                return None
    return host, raw


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
    # the ticket's clock is monotonic; expires_at is wall time for display
    # only — the GUI counts down from ttl_seconds, not its own clock
    # the installer line points at the SAME address as the login line: built
    # in the browser from its own origin it said `localhost` whenever the
    # operator was browsing on the box, which the other computer cannot reach
    base = address if address.startswith("https://") else f"http://{address}"
    return {"login": f"address={address} code={code}", "address": address,
            "install": f"curl -fsSL {base}/cli/install.sh | sh",
            "code": code, "name": t.name,
            "expires_at": time.time() + pastelogin.TTL_SECONDS,
            "ttl_seconds": pastelogin.TTL_SECONDS,
            "plain_http": not address.startswith("https://")}


@router.delete("/login-code")
async def cancel_login_code(user: dict = Depends(require_user)):
    """Settings "Done" / leaving the page: a dismissed code stops working now
    rather than at the end of its TTL. Drops every live code this user
    minted."""
    return {"ok": True, "cancelled": pastelogin.cancel(user["username"])}


@router.get("")
async def list_devices():
    """Computers with live tokens (never the token itself), each with its
    expires_at, last_used_at and idle deadline (idle_expires_at)."""
    return {"devices": await devicetokens.list_tokens(),
            "ttl_days": settings.device_token_ttl_days,
            "idle_days": settings.device_token_idle_days}


@router.delete("/{token_id:int}")
async def revoke_device(token_id: int):
    stopped = await _stop_device_turns(token_id)
    if not await devicetokens.revoke(token_id):
        raise HTTPException(status_code=404, detail="no such device token")
    return {"ok": True, "stopped_turns": stopped}


async def _stop_device_turns(token_id: int) -> int:
    """require_actor runs once, at request start, and the turn it admitted is
    a detached task — so revoking the credential has to end what it started
    as well, or a revoked computer's turn keeps calling tools. Cancelling
    first is harmless if the id turns out not to exist (it started nothing).
    A desk token started no turns but may hold a live socket: that is dropped
    too, and the turns driving it are stopped (desk.disconnect)."""
    from . import chat, desk
    n = chat.stop_actor_turns(chat.device_actor(token_id))
    return n + await desk.disconnect(token_id, reason="token revoked")


# --- device side ----------------------------------------------------------------

class RedeemBody(BaseModel):
    code: str = Field(..., max_length=256)
    name: str = Field("", max_length=256)
    hostname: str = Field("", max_length=256)
    platform: str = Field("", max_length=256)
    # what the token will be for: `jav3` asks for cli (the default), `jav3-desk`
    # for desk. Letting the client choose is safe because neither widens the
    # other: a desk token reaches nothing but its socket, and a desk does
    # nothing until the operator grants it in Settings.
    scope: str = Field("cli", pattern="^(cli|desk)$")


MAX_LOGIN_BODY = 4096
_BAD_BODY = "malformed login request"


async def _login_body(request: Request) -> RedeemBody:
    """The redeem body, read by hand so an unauthenticated caller cannot make
    the server buffer and parse megabytes: a Content-Length over the cap is a
    413 before a byte is read, and a chunked body is cut off at the cap. A
    parse/validation failure is one fixed 422 that echoes nothing (FastAPI's
    stock 422 reflects the input back, oversized or not)."""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise HTTPException(status_code=400, detail=_BAD_BODY)
    if declared > MAX_LOGIN_BODY:
        raise HTTPException(status_code=413, detail="request too large")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_LOGIN_BODY:
            raise HTTPException(status_code=413, detail="request too large")
    try:
        return RedeemBody.model_validate(json.loads(bytes(buf)))
    except (ValueError, ValidationError):
        raise HTTPException(status_code=422, detail=_BAD_BODY)


@pair_router.post("/login")
async def redeem_login_code(request: Request, response: Response,
                            body: RedeemBody = Depends(_login_body)):
    """Trade a login code for a device token — once.

    Every failure is the same 401 with the same text, whether the code was
    malformed, never issued, expired or already used. `redeem` is synchronous
    and pops the ticket, so the spend happens before the first await below.

    The code is looked up BEFORE the throttle is consulted: a valid code always
    redeems, and only a miss can be answered 429 (see pastelogin's docstring).
    The body cap in `_login_body` bounds what a miss costs before this runs.
    """
    t = pastelogin.redeem(body.code)
    peer = _peer(request)
    if t is None:
        _charge_miss(peer)
        raise HTTPException(status_code=401, detail=_BAD_CODE)
    name = t.name or body.name.strip()[:64] or body.hostname.strip()[:64] or "cli"
    raw, tid = await devicetokens.mint(name, hostname=body.hostname,
                                       platform=body.platform, by=t.by,
                                       scope=body.scope)
    db = None
    try:
        # opening the DB is inside the try too: SQLITE_BUSY here, after the
        # token row committed, would 500 and orphan a live token nobody holds
        db = await get_db()
        await security.raise_event(
            db, kind="device_enrolled", severity="info",
            summary=f"computer '{name}' logged in with a code minted by "
                    f"{t.by or '?'} (from {peer})",
            detail={"device_id": tid, "name": name,
                    "hostname": body.hostname[:128], "platform": body.platform[:32],
                    "scope": body.scope,
                    "peer": peer, "minted_by": t.by,
                    # the ticket clock is monotonic; report wall time
                    "minted_at": time.time() - (time.monotonic() - t.created),
                    "agent": request.headers.get("user-agent", "")[:120]})
    except Exception:  # noqa: BLE001 — the audit line must not eat the token
        pass
    finally:
        if db is not None:
            await db.close()
    response.headers.update(_NO_STORE)
    return {"ok": True, "token": raw, "device_id": tid, "name": name,
            "scope": body.scope}


@pair_router.get("/whoami")
async def whoami(actor: dict = Depends(require_any_actor)):
    """Echo the authenticated actor — the operator cookie or a device token."""
    return actor


@pair_router.delete("/self")
async def revoke_self(actor: dict = Depends(require_any_actor)):
    """`jav3 logout`: a device revokes its own token. Only ever the presenting
    token — there is no id parameter to point at another one."""
    if not actor.get("is_device"):
        raise HTTPException(status_code=400, detail="only a device token can "
                            "revoke itself; use Settings → Devices")
    await devicetokens.revoke(actor["device_id"])
    return {"ok": True, "stopped_turns": await _stop_device_turns(actor["device_id"])}


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
