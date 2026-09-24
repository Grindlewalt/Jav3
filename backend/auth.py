import asyncio
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .config import settings, get_jwt_secret
from .db import get_db

COOKIE_NAME = "jarvis_token"

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Login throttling.
#
# This endpoint is the front door, and when Jarvis is published it is the one
# thing an attacker can reach without a credential. bcrypt already caps guessing
# at tens per second rather than millions, but that is a slow no rather than a
# no, so failures now cost increasing time.
#
# Two deliberate choices:
#
# Keyed on the USERNAME, not the IP. Behind a tunnel (cloudflared, any reverse
# proxy) every request arrives from the proxy's address, so per-IP counting both
# fails to separate attackers and lets one of them throttle the operator by
# filling a shared bucket. A username key is unaffected by what sits in front.
#
# A growing DELAY, never a lockout. On a one-operator system a lockout is a
# self-denial: anyone who knows the username could lock the owner out of their
# own house. Waiting eight seconds is an inconvenience; being unable to log in
# at all is the attack succeeding by another route.
#
# And the delay is charged AFTER the password is checked, only on failure. The
# first cut paid it up front, so an attacker who had run the counter up made the
# operator's own correct login wait eight seconds too — measured at 8.22s, which
# is the self-denial this was supposed to avoid. Charging it on the way out
# costs an attacker exactly the same (they learn nothing until the response
# arrives, so hanging up early buys them nothing) and costs the operator
# nothing.
_FAIL_WINDOW = 900.0          # failures older than this are forgotten
_DELAY_CAP = 8.0
_ALERT_AT = 5
_failures: dict[str, list] = {}          # key -> [count, first, last, alerted]
_GLOBAL = "*"                            # spraying across usernames still slows

# Checked against when the username does not exist, so a miss costs the same
# time as a wrong password and the response cannot be used to enumerate users.
# Precomputed rather than generated on first use: generating it made the first
# unknown-username attempt about twice as slow as the rest, which is its own
# small oracle. It is a hash of a value no password can produce, and is not a
# secret.
_DUMMY_HASH = "$2b$12$/JH/yp7Ilo3fBIFXu6aeaOE5RVMSzKRv/mvaKWR3t8GKHHHEq9/CO"


def _delay_for(count: int) -> float:
    """0, 0, 0.5, 1, 2, 4, 8, 8, ... — the first failure is free (typo).

    The exponent is clamped before the shift, not after. Computing 2**(count-2)
    first and capping the result works fine until enough concurrent attempts
    push the counter past ~1024, where the intermediate overflows a float and
    the endpoint answers 500 instead of 401 — a crash reachable by exactly the
    traffic this function exists to slow down.
    """
    if count < 2:
        return 0.0
    steps = min(count - 2, 20)
    return min(_DELAY_CAP, 0.5 * (2 ** steps))


def _record(key: str, now: float) -> list:
    count, first, last, alerted = _failures.get(key, [0, now, now, False])
    if now - last > _FAIL_WINDOW:
        count, first, alerted = 0, now, False
    count += 1
    state = [count, first, now, alerted]
    _failures[key] = state
    return state


def _clear(key: str) -> None:
    _failures.pop(key, None)


def _peer(request: Request) -> str:
    """The TCP peer, for the alert text only. Forwarding headers are not read:
    with no trusted proxy in front of a LAN server they are whatever the caller
    chose to send."""
    return (getattr(request.client, "host", None) or "?")[:64]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def make_token(user_id: int, username: str) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_ttl_hours),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm="HS256")


def user_from_token(token: str | None) -> dict | None:
    """Validate a session token -> user dict, or None. Shared by the HTTP
    dependency and the WebSocket path (which can't use Depends(require_user))."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    return {"id": int(payload["sub"]), "username": payload["username"]}


def require_user(request: Request) -> dict:
    user = user_from_token(request.cookies.get(COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


# ---------------------------------------------------------------------------
# Same-origin (CSRF) check — applied GLOBALLY by SameOriginMiddleware.
#
# SameSite=Lax keeps the cookie off cross-SITE requests, but "site" ignores the
# port and the scheme: any other service on this box's name/IP (another port,
# a companion app, anything a local process binds) is same-site and gets the
# cookie on a form POST or a WebSocket handshake. So every cookie-carrying
# state change is checked against the full origin — scheme, host AND port —
# not just the hostname.

_DEFAULT_PORT = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
# Routes with no cookie authentication of their own: the device redeem (a
# code in the body), the CLI files, and git-over-HTTP (Basic auth).
_ORIGIN_EXEMPT_EXACT = {"/api/devices/login"}
_ORIGIN_EXEMPT_PREFIX = ("/cli/", "/git/")


def _split_origin(value: str) -> tuple[str, str, int] | None:
    """'scheme://host[:port]' -> (scheme, host, port), IPv6-safe; None for
    'null', a path-bearing or credential-bearing value, or anything odd."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(value.strip())
        port = u.port
    except ValueError:
        return None
    if (u.scheme not in ("http", "https") or not u.hostname or u.username
            or u.password or u.query or u.fragment or u.path not in ("", "/")):
        return None
    return u.scheme, u.hostname.lower(), port or _DEFAULT_PORT[u.scheme]


def _own_origins(host_header: str, scheme: str) -> set[tuple[str, str, int]]:
    """Every origin this request may legitimately come from: the Host it was
    sent to (under the transport scheme, and https too when cookie_secure says
    TLS terminates in front), the operator's explicit csrf_allowed_hosts, and
    the server's own LAN names — the latter ONLY on the server's own port."""
    from . import lan
    schemes = {"https" if scheme in ("https", "wss") else "http"}
    if settings.cookie_secure:
        schemes.add("https")
    out: set[tuple[str, str, int]] = set()
    for s in schemes:
        me = _split_origin(f"{s}://{host_header}") if host_header else None
        if me:
            out.add(me)
        for h in lan.own_hosts():
            hh = f"[{h}]" if ":" in h else h
            o = _split_origin(f"{s}://{hh}:{settings.lan_port}")
            if o:
                out.add(o)
    return out


def _explicit_allowed(o: tuple[str, str, int]) -> bool:
    """settings.csrf_allowed_hosts: the operator's own list (a reverse proxy's
    name, say). An entry is a bare host (any port — the operator said so) or
    host:port."""
    from urllib.parse import urlsplit
    for entry in settings.csrf_allowed_hosts:
        e = entry.strip().lower()
        if not e:
            continue
        try:
            u = urlsplit(e if "://" in e else "//" + e)
            port = u.port
        except ValueError:
            continue
        if (u.hostname == o[1] and (port is None or port == o[2])
                and (not u.scheme or u.scheme == o[0])):
            return True
    return False


def origin_allowed(headers, scheme: str) -> bool:
    """The check itself, on a request's headers. A missing Origin is allowed
    (curl, the CLI — no ambient cookie to abuse) unless the browser told us
    otherwise: a Referer from elsewhere, or Sec-Fetch-Site saying the request
    was not same-origin."""
    host = (headers.get("host") or "").strip()
    fetch_site = (headers.get("sec-fetch-site") or "").lower()
    if fetch_site in ("cross-site", "same-site"):
        return False
    origin = headers.get("origin")
    if origin is None:
        referer = headers.get("referer")
        if not referer:
            return True
        from urllib.parse import urlsplit
        try:
            r = urlsplit(referer)
            origin = f"{r.scheme}://{r.netloc}"
        except ValueError:
            return False
    o = _split_origin(origin)
    if o is None:
        return False
    return o in _own_origins(host, scheme) or _explicit_allowed(o)


def require_same_origin(request: Request) -> None:
    """`origin_allowed` as a dependency-shaped check (403 on failure). Routes
    do not need it — SameOriginMiddleware applies it to every cookie-carrying
    state change and WebSocket handshake."""
    if not origin_allowed(request.headers, request.scope.get("scheme", "http")):
        raise HTTPException(status_code=403, detail="cross-origin request refused")


class SameOriginMiddleware:
    """One CSRF gate for the whole app (pure ASGI, so it sees WebSocket
    handshakes too): a request that CARRIES the session cookie and changes
    state — POST/PUT/PATCH/DELETE, or any WebSocket upgrade — must come from
    this app's own origin. Requests without the cookie (the CLI's bearer, git
    Basic, the unauthenticated redeem) have no ambient authority to abuse and
    pass untouched; the unauthenticated routes are exempt outright."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind in ("http", "websocket") and self._must_check(scope):
            from starlette.requests import HTTPConnection
            conn = HTTPConnection(scope)
            if COOKIE_NAME in conn.cookies and not origin_allowed(
                    conn.headers, scope.get("scheme", "http")):
                if kind == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                from starlette.responses import JSONResponse
                await JSONResponse({"detail": "cross-origin request refused"},
                                   status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _must_check(scope) -> bool:
        path = scope.get("path", "")
        if path in _ORIGIN_EXEMPT_EXACT or path.startswith(_ORIGIN_EXEMPT_PREFIX):
            return False
        return scope["type"] == "websocket" or scope.get("method") in _UNSAFE


async def require_actor(request: Request) -> dict:
    """Operator cookie session OR an enrolled device's Bearer token.

    For the routers a device/CLI is allowed to reach. The cookie path is checked
    first and stays sync + DB-free, so a browser request pays nothing extra; only
    a cookieless request touches the token store. A device actor is marked
    `is_device` and carries no real user id (id=-1) — the sensitive control-plane
    routers keep `require_user` (cookie only) and never see a device token.
    """
    user = user_from_token(request.cookies.get(COOKIE_NAME))
    if user is not None:
        return user
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        from . import devicetokens
        dev = await devicetokens.verify(header[7:].strip())
        if dev is not None:
            return {"id": -1, "username": f"device:{dev['name']}",
                    "is_device": True, "device_id": dev["device_id"]}
    raise HTTPException(status_code=401, detail="not authenticated")


async def _alert(key: str, count: int, peer: str, via: str = "login") -> None:
    """One security event per burst, so a scanner is a line in the Review Center
    rather than a flood that hides everything else."""
    try:
        from . import security
        db = await get_db()
        try:
            await security.raise_event(
                db, kind="login_failed", severity="warn",
                summary=f"{count} failed logins for '{key}' via {via} (from {peer})",
                detail={"username": key, "attempts": count, "peer": peer, "via": via,
                        "note": "each further attempt is delayed up to 8s. The "
                                "peer is the TCP peer and is not used for "
                                "throttling."})
        finally:
            await db.close()
    except Exception:
        pass          # an alert must never be able to break logging in


async def check_password_login(username: str, password: str, request: Request,
                               via: str = "login"):
    """The one password check: the users row on success, else None — after
    charging the failure to the username (and global) counters, alerting once
    per burst and sleeping the growing delay. Shared by the GUI login and
    git-over-HTTP Basic, so neither is an unmetered password oracle."""
    key = (username or "").strip().lower()[:80]
    now = time.time()
    db = await get_db()
    try:
        async with db.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (username,),
        ) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()

    if row is None:
        # spend the same time as a real check: skipping bcrypt here made a
        # missing username measurably faster to reject than a wrong password,
        # which is a free list of who has an account
        verify_password(password, _DUMMY_HASH)
        ok = False
    else:
        ok = verify_password(password, row["password_hash"])

    if not ok:
        count, first, _, alerted = _record(key, now)
        sprayed = _record(_GLOBAL, now)[0]
        if count >= _ALERT_AT and not alerted:
            _failures[key][3] = True
            await _alert(key, count, _peer(request), via)
        # charged on the way out: the attempt is already known to be wrong, so
        # this cost lands only on failures and never on the operator
        delay = max(_delay_for(count), _delay_for(sprayed // 3))
        if delay:
            await asyncio.sleep(delay)
        return None
    _clear(key)
    _clear(_GLOBAL)
    return row


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    row = await check_password_login(body.username, body.password, request)
    if row is None:
        raise HTTPException(status_code=401, detail="bad credentials")
    response.set_cookie(
        COOKIE_NAME,
        make_token(row["id"], row["username"]),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.jwt_ttl_hours * 3600,
    )
    return {"ok": True, "username": row["username"]}


@router.post("/logout")
async def logout(response: Response):
    # mirror the set-cookie attributes so deletion keeps matching if a Domain/
    # Path is ever added to the login cookie
    response.delete_cookie(COOKIE_NAME, samesite="lax",
                           secure=settings.cookie_secure)
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(require_user)):
    return user
