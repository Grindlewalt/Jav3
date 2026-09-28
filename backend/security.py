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

_COLUMNS = ("id, kind, severity, project_slug, summary, detail, acknowledged, "
            "created_at, acknowledged_at, triage_verdict, triage_reason")


def _row(r) -> dict:
    """A row with its detail decoded. Undecodable JSON is handed back as text
    rather than raised: one malformed blob must not take down the whole queue."""
    d = dict(r)
    raw = d.get("detail")
    if raw:
        try:
            d["detail"] = json.loads(raw)
        except ValueError:
            d["detail"] = {"unparsed": str(raw)[:2000]}
    else:
        d["detail"] = None
    return d


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
    q = f"SELECT {_COLUMNS} FROM security_events"
    if unacknowledged_only:
        q += " WHERE acknowledged = 0"
    q += " ORDER BY id DESC LIMIT ?"
    async with db.execute(q, (limit,)) as cur:
        return [_row(r) for r in await cur.fetchall()]


async def get_event(db: aiosqlite.Connection, event_id: int) -> dict | None:
    """One event by id, acknowledged or not — the context board is reachable
    from a toast long after the row left the unacknowledged queue."""
    async with db.execute(f"SELECT {_COLUMNS} FROM security_events WHERE id = ?",
                          (event_id,)) as cur:
        r = await cur.fetchone()
    return _row(r) if r is not None else None


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


# --- harness self-report (report_harness_fault) ------------------------------
# TEMPORARY diagnostic surface: an agent logs when the HARNESS misbehaved (a
# tool that errored on input it believed valid, a documented capability that
# didn't do what it says). Stored in its own table AND mirrored to a
# low-severity security event so it is reviewable beside the other alerts.

_FAULT_COLUMNS = ("id, conversation_id, project, tool, tried, went_wrong, "
                  "expected, severity, created_at")


async def record_harness_fault(db: aiosqlite.Connection, *, tried: str,
                                went_wrong: str, expected: str | None = None,
                                tool: str | None = None, severity: str = "low",
                                conversation_id: int | None = None,
                                project: str | None = None) -> int:
    """Persist one harness-fault report and raise a matching security event.

    The row is the durable record; the event is what surfaces it in the bell
    and Review Center. A security event's severity is info|warn|critical, so a
    'low' harness fault maps to 'info' there while the row keeps 'low' verbatim.
    """
    cur = await db.execute(
        "INSERT INTO harness_faults (conversation_id, project, tool, tried, "
        "went_wrong, expected, severity) VALUES (?,?,?,?,?,?,?)",
        (conversation_id, project, tool, tried, went_wrong, expected, severity))
    await db.commit()
    fault_id = cur.lastrowid
    head = (tool + ": " if tool else "") + went_wrong
    await raise_event(
        db, kind="harness_fault", severity="info",
        project=project, summary=f"Harness fault reported: {head[:160]}",
        detail={"fault_id": fault_id, "conversation_id": conversation_id,
                "tool": tool, "tried": tried, "went_wrong": went_wrong,
                "expected": expected, "severity": severity})
    return fault_id


async def list_harness_faults(db: aiosqlite.Connection, *,
                              limit: int = 100) -> list[dict]:
    async with db.execute(
        f"SELECT {_FAULT_COLUMNS} FROM harness_faults ORDER BY id DESC LIMIT ?",
        (limit,)) as cur:
        return [dict(r) for r in await cur.fetchall()]
