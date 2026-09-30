"""The host gateway's refusal log (Security > Calls in the terminal client).

Every place the gateway turns a guest's request away (an op_id that is not the
caller's turn, a spent budget, an op the box's kind may not use) leaves one
short row here, next to the model_calls ledger of what it did serve. The fields
come from the host's own identity for the caller (the box, the bound turn's
project); the op name and reason are truncated to printable text because the
op name is whatever the guest typed. Best effort: a failed write must never
change what the gateway answers.
"""
import time

from ..db import get_db

# a compromised guest can ask for a refusal as fast as it can send lines; the
# log keeps the newest rows only
KEEP = 5000
# ...and only this many rows a second per box are written at all (a burst of
# 30, then 5 a second): each row is a DB write, and a hostile guest can ask for
# a refusal as fast as it can send a line
ROWS_PER_S = 5.0
ROWS_BURST = 30
_buckets: dict[str, list] = {}         # box key -> [tokens, last refill]
# a cap trip raises one security event per box and cap per this many seconds
TRIP_EVERY_S = 10.0
_trips: dict[tuple, float] = {}


def _row_allowed(key: str) -> bool:
    now = time.monotonic()
    b = _buckets.get(key)
    if b is None:
        if len(_buckets) > 512:
            _buckets.clear()
        b = _buckets[key] = [float(ROWS_BURST), now]
    b[0] = min(float(ROWS_BURST), b[0] + (now - b[1]) * ROWS_PER_S)
    b[1] = now
    if b[0] < 1.0:
        return False
    b[0] -= 1.0
    return True


def _clean(v, n: int = 80) -> str | None:
    if v is None:
        return None
    s = "".join(ch for ch in str(v) if ch.isprintable())[:n]
    return s or None


async def record_refusal(op_name, reason: str, box_id=None,
                         project_slug=None) -> None:
    if not _row_allowed(str(box_id or "?")):
        return
    try:
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO gateway_refusals (op_name, reason, box_id, project_slug) "
                "VALUES (?, ?, ?, ?)",
                (_clean(op_name), _clean(reason) or "refused", _clean(box_id),
                 _clean(project_slug)))
            await db.execute(
                "DELETE FROM gateway_refusals WHERE id <= "
                "(SELECT MAX(id) FROM gateway_refusals) - ?", (KEEP,))
            await db.commit()
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — the log must never fail a gateway reply
        pass


async def record_cap_trip(cap: str, message: str, box_id=None, project_slug=None,
                          detail: dict | None = None) -> None:
    """A hard cap on what the guest may make the host hold tripped (a request
    line too large, too many connections, a spent buffer budget...). One
    refusal row, and one security event that counts its repeats, per box and
    cap every TRIP_EVERY_S seconds. Best effort, like the refusal log."""
    now = time.monotonic()
    key = (str(box_id or "?"), cap)
    if now - _trips.get(key, -1e9) < TRIP_EVERY_S:
        return
    if len(_trips) > 512:
        _trips.clear()
    _trips[key] = now
    await record_refusal("gateway", f"cap:{cap}", box_id, project_slug)
    try:
        from .. import security
        db = await get_db()
        try:
            await security.raise_event(
                db, kind="gateway_cap", severity="warn", project=project_slug,
                summary=f"Gateway cap tripped ({cap}) by {box_id or 'an unknown box'}: {message}",
                detail={"cap": cap, "box": box_id, **(detail or {})},
                cause=f"gateway_cap:{box_id}:{cap}")
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — as above
        pass
