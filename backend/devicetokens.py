"""API tokens for logged-in computers/CLIs.

A token is a high-entropy `jvd_<token_urlsafe(32)>` string minted when a CLI
redeems a login code (`backend/pastelogin.py`) and shown to it exactly once. Only its sha256 is
stored, so the DB never holds anything usable if it leaks; verification hashes
the presented token and looks the hash up. Tokens are individually revocable and
carry a `last_seen` so the operator can spot a stale or rogue one.

This is a bearer credential with (currently) operator-equivalent reach on the
routes that opt into `auth.require_actor`; it is deliberately NOT accepted on the
sensitive control-plane routers (secrets, vm, egress, …), which
stay cookie-only.
"""
import hashlib
import secrets as _secrets

from .db import get_db

PREFIX = "jvd_"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def mint(name: str, *, hostname: str = "", platform: str = "",
               by: str = "") -> tuple[str, int]:
    """Create a token; return (raw_token, id). The raw token is returned ONCE
    and never stored — only its hash lands in the DB."""
    raw = PREFIX + _secrets.token_urlsafe(32)
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO device_tokens (name, token_hash, hostname, platform, "
            "paired_by) VALUES (?,?,?,?,?)",
            ((name or "").strip()[:64] or "device", _hash(raw),
             (hostname or "").strip()[:128] or None,
             (platform or "").strip()[:32] or None,
             (by or "").strip()[:64] or None))
        await db.commit()
        return raw, cur.lastrowid
    finally:
        await db.close()


async def verify(raw: str | None) -> dict | None:
    """A presented bearer token -> {'device_id', 'name'} if live, else None.
    Touches last_seen on a hit. A malformed/short token is rejected before any
    DB work; the lookup is an exact match on the token's sha256."""
    if not raw or not raw.startswith(PREFIX) or len(raw) < len(PREFIX) + 20:
        return None
    db = await get_db()
    try:
        async with db.execute(
                "SELECT id, name FROM device_tokens "
                "WHERE token_hash = ? AND revoked = 0", (_hash(raw),)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        await db.execute("UPDATE device_tokens SET last_seen = datetime('now') "
                         "WHERE id = ?", (row["id"],))
        await db.commit()
        return {"device_id": row["id"], "name": row["name"]}
    finally:
        await db.close()


async def list_tokens() -> list[dict]:
    db = await get_db()
    try:
        async with db.execute(
                "SELECT id, name, hostname, platform, paired_by, created_at, "
                "last_seen, revoked FROM device_tokens WHERE revoked = 0 "
                "ORDER BY created_at DESC") as cur:
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
