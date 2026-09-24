"""API tokens for logged-in computers/CLIs.

A token is a high-entropy `jvd_<token_urlsafe(32)>` string minted when a CLI
redeems a login code (`backend/pastelogin.py`) and shown to it exactly once. Only its sha256 is
stored, so the DB never holds anything usable if it leaks; verification hashes
the presented token and looks the hash up.

A token is not forever. It stops verifying when any of these holds:
- it was revoked (Settings -> Devices, or `jav3 logout`);
- `expires_at` has passed (`device_token_ttl_days` after minting);
- it went unused for `device_token_idle_days` (`last_used_at`);
- the user it was minted for no longer exists (`user_id` joins `users`).
All four look the same to the caller: no row, one 401.

This is a bearer credential with (currently) operator-equivalent reach on the
routes that opt into `auth.require_actor`; it is deliberately NOT accepted on the
sensitive control-plane routers (secrets, vm, egress, …), which
stay cookie-only.
"""
import hashlib
import secrets as _secrets

from .config import settings
from .db import get_db

PREFIX = "jvd_"
# last_used_at is written at most this often: verify runs on every CLI
# request, and the idle clock only needs minute resolution.
TOUCH_EVERY_SECONDS = 60


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def mint(name: str, *, hostname: str = "", platform: str = "",
               by: str = "") -> tuple[str, int]:
    """Create a token; return (raw_token, id). The raw token is returned ONCE
    and never stored — only its hash lands in the DB. `by` is the username of
    the session that minted the login code; the token belongs to that user's
    id and dies with the account."""
    raw = PREFIX + _secrets.token_urlsafe(32)
    by = (by or "").strip()[:64]
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO device_tokens (name, token_hash, hostname, platform, "
            "paired_by, user_id, expires_at) VALUES (?,?,?,?,?,"
            "(SELECT id FROM users WHERE username = ?), datetime('now', ?))",
            ((name or "").strip()[:64] or "device", _hash(raw),
             (hostname or "").strip()[:128] or None,
             (platform or "").strip()[:32] or None,
             by or None, by, f"+{int(settings.device_token_ttl_days)} days"))
        await db.commit()
        return raw, cur.lastrowid
    finally:
        await db.close()


async def verify(raw: str | None) -> dict | None:
    """A presented bearer token -> {'device_id', 'name'} if live, else None.
    A malformed/short token is rejected before any DB work; the lookup is an
    exact match on the token's sha256, and revoked / expired / idle / orphaned
    are all the same miss. Touches last_used_at at most once a minute."""
    if not raw or not raw.startswith(PREFIX) or len(raw) < len(PREFIX) + 20:
        return None
    db = await get_db()
    try:
        async with db.execute(
                "SELECT t.id, t.name FROM device_tokens t "
                "JOIN users u ON u.id = t.user_id "
                "WHERE t.token_hash = ? AND t.revoked = 0 "
                "AND t.expires_at > datetime('now') "
                "AND COALESCE(t.last_used_at, t.created_at) > datetime('now', ?)",
                (_hash(raw), f"-{int(settings.device_token_idle_days)} days")) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        cur = await db.execute(
            "UPDATE device_tokens SET last_used_at = datetime('now') WHERE id = ? "
            "AND (last_used_at IS NULL OR last_used_at <= datetime('now', ?))",
            (row["id"], f"-{TOUCH_EVERY_SECONDS} seconds"))
        if cur.rowcount:
            await db.commit()
        return {"device_id": row["id"], "name": row["name"]}
    finally:
        await db.close()


async def list_tokens() -> list[dict]:
    """Live tokens with their lifetime: expires_at, last_used_at and the idle
    deadline (idle_expires_at). A token past either deadline is not listed —
    it no longer verifies."""
    db = await get_db()
    try:
        async with db.execute(
                "SELECT t.id, t.name, t.hostname, t.platform, t.paired_by, "
                "t.created_at, t.expires_at, t.last_used_at, "
                "datetime(COALESCE(t.last_used_at, t.created_at), ?) AS idle_expires_at "
                "FROM device_tokens t JOIN users u ON u.id = t.user_id "
                "WHERE t.revoked = 0 AND t.expires_at > datetime('now') "
                "AND COALESCE(t.last_used_at, t.created_at) > datetime('now', ?) "
                "ORDER BY t.created_at DESC",
                (f"+{int(settings.device_token_idle_days)} days",
                 f"-{int(settings.device_token_idle_days)} days")) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def revoke(token_id: int) -> bool:
    db = await get_db()
    try:
        cur = await db.execute(
            "UPDATE device_tokens SET revoked = 1 WHERE id = ? AND revoked = 0",
            (token_id,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()
