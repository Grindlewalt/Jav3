"""security_events — persisted, acknowledgeable security alerts.

Distinct from notifications_api's poll-derived *standing-state* aggregate: those
recompute "what is pending right now" every poll and have no memory. These are
*transient events* (an anomaly fired, a host was cut, a diff gate tripped, a
secret leaked into a staged file, the golden image went stale) that must survive
the moment they happened and be acknowledged one by one. The bell and the Review
Center read this; anomaly/diffgate/image-staleness write it.
"""
import json

import aiosqlite

from . import bus

SECURITY_CHAN = "security"       # bus channel the bell + Review Center subscribe to


async def raise_event(db: aiosqlite.Connection, *, kind: str, summary: str,
                      severity: str = "warn", project: str | None = None,
                      detail: dict | None = None) -> int:
    cur = await db.execute(
        "INSERT INTO security_events(kind, severity, project_slug, summary, detail) "
        "VALUES (?,?,?,?,?)",
        (kind, severity, project, summary, json.dumps(detail) if detail is not None else None))
    await db.commit()
    # mirror the REST row shape (detail as an object) so live-SSE rows in the
    # Review Center render the same as poll-loaded ones
    bus.publish(SECURITY_CHAN, {"type": "security_event", "id": cur.lastrowid,
                                "kind": kind, "severity": severity, "project": project,
                                "summary": summary, "detail": detail})
    return cur.lastrowid


async def list_events(db: aiosqlite.Connection, *, unacknowledged_only: bool = False,
                      limit: int = 100) -> list[dict]:
    q = ("SELECT id, kind, severity, project_slug, summary, detail, acknowledged, "
         "created_at, acknowledged_at, triage_verdict, triage_reason "
         "FROM security_events")
    if unacknowledged_only:
        q += " WHERE acknowledged = 0"
    q += " ORDER BY id DESC LIMIT ?"
    async with db.execute(q, (limit,)) as cur:
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out


async def acknowledge(db: aiosqlite.Connection, event_id: int) -> dict:
    await db.execute("UPDATE security_events SET acknowledged=1, "
                     "acknowledged_at=datetime('now') WHERE id = ?", (event_id,))
    await db.commit()
    return {"ok": True}


async def acknowledge_all(db: aiosqlite.Connection) -> dict:
    """Operator bulk-clear — the queue reached hundreds in practice."""
    cur = await db.execute("UPDATE security_events SET acknowledged=1, "
                           "acknowledged_at=datetime('now') WHERE acknowledged=0")
    await db.commit()
    return {"ok": True, "done": cur.rowcount}


async def count_unacknowledged(db: aiosqlite.Connection) -> int:
    async with db.execute(
            "SELECT COUNT(*) AS n FROM security_events WHERE acknowledged = 0") as cur:
        return (await cur.fetchone())["n"]
