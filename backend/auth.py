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
    """For the alert text only — never for a throttling decision. These headers
    are trivially forged by anything that can reach the app directly, so they
    are a hint about who, not an input to the control."""
    for h in ("cf-connecting-ip", "x-forwarded-for"):
        v = request.headers.get(h)
        if v:
            return v.split(",")[0].strip()[:64]
    return getattr(request.client, "host", "?")


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


async def _alert(key: str, count: int, peer: str) -> None:
    """One security event per burst, so a scanner is a line in the Review Center
    rather than a flood that hides everything else."""
    try:
        from . import security
        db = await get_db()
        try:
            await security.raise_event(
                db, kind="login_failed", severity="warn",
                summary=f"{count} failed logins for '{key}' (from {peer})",
                detail={"username": key, "attempts": count, "peer": peer,
                        "note": "each further attempt is delayed up to 8s. The "
                                "peer is taken from CF-Connecting-IP/"
                                "X-Forwarded-For where present and is a hint "
                                "only — it is not used for throttling."})
        finally:
            await db.close()
    except Exception:
        pass          # an alert must never be able to break logging in


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    key = (body.username or "").strip().lower()[:80]
    now = time.time()
    db = await get_db()
    try:
        async with db.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (body.username,),
        ) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()

    if row is None:
        # spend the same time as a real check: skipping bcrypt here made a
        # missing username measurably faster to reject than a wrong password,
        # which is a free list of who has an account
        verify_password(body.password, _DUMMY_HASH)
        ok = False
    else:
        ok = verify_password(body.password, row["password_hash"])

    if not ok:
        count, first, _, alerted = _record(key, now)
        sprayed = _record(_GLOBAL, now)[0]
        if count >= _ALERT_AT and not alerted:
            _failures[key][3] = True
            await _alert(key, count, _peer(request))
        # charged on the way out: the attempt is already known to be wrong, so
        # this cost lands only on failures and never on the operator
        delay = max(_delay_for(count), _delay_for(sprayed // 3))
        if delay:
            await asyncio.sleep(delay)
        raise HTTPException(status_code=401, detail="bad credentials")

    _clear(key)
    _clear(_GLOBAL)
    response.set_cookie(
        COOKIE_NAME,
        make_token(row["id"], row["username"]),
        httponly=True,
        samesite="lax",
        max_age=settings.jwt_ttl_hours * 3600,
    )
    return {"ok": True, "username": row["username"]}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(require_user)):
    return user
