"""security_events — persisted, acknowledgeable security alerts.

Distinct from notifications_api's poll-derived *standing-state* aggregate: those
recompute "what is pending right now" every poll and have no memory. These are
*transient events* (an anomaly fired, a host was cut, a diff gate tripped, a
secret leaked into a staged file, the golden image went stale) that must survive
the moment they happened and be acknowledged one by one. The bell and the Review
Center read this; anomaly/diffgate/image-staleness write it.

Pings. Every event is recorded; only some interrupt the operator. The server
decides, once, and stamps the live event with `ping` so the web toasts and
the terminal client's sidebar agree:

  tier       what                                         pings at level
  critical   severity critical, or an ALWAYS kind         every level
  approval   a request that waits on the operator         approvals, all
  alert      warn                                         all
  record     info: an audit line, nothing to do           never

The level (Settings -> Notifications) defaults to "approvals". A repeat of an
unacknowledged event inside `security_coalesce_seconds` bumps that row's
count instead of adding a row, and pings only if it is critical (at most once
per ping window). Non-critical pings are rate limited per kind. Nothing here
changes a severity, acknowledges or approves anything: quieter, same record.
"""
import json
import time
from collections import deque
from datetime import datetime, timezone

import aiosqlite

from . import bus
from .config import settings

SECURITY_CHAN = "security"       # bus channel the bell + Review Center subscribe to

NOTIFY_KEY = "notify_level"      # session_state: the operator's ping level
LEVELS = ("critical", "approvals", "all")
DEFAULT_LEVEL = "approvals"
# requests that wait on an operator decision in their own queue (services,
# packages): they ping at the default level whatever severity they carry
APPROVAL_KINDS = frozenset({"service_requested", "package_requested"})
# ping at every level even when raised below critical: an anomaly cut, a
# refused secret leak, a guest connection no reported process owns
ALWAYS_KINDS = frozenset({"egress_anomaly", "host_cut", "secret_leak",
                          "proc_report_mismatch"})

_COLUMNS = ("id, kind, severity, project_slug, summary, detail, acknowledged, "
            "created_at, acknowledged_at, triage_verdict, triage_reason, "
            "count, last_seen, cause")


def tier(kind: str | None, severity: str | None) -> str:
    """critical | approval | alert | record (see the module doc)."""
    if severity == "critical" or kind in ALWAYS_KINDS:
        return "critical"
    if kind in APPROVAL_KINDS:
        return "approval"
    return "record" if severity == "info" else "alert"


def wants(t: str, level: str) -> bool:
    """Does an event of tier `t` ping at this level?"""
    if t == "critical":
        return True
    if t == "approval":
        return level in ("approvals", "all")
    return t == "alert" and level == "all"


def _sql_list(xs) -> str:
    return ",".join("'" + x + "'" for x in sorted(xs))


# tier() as SQL, for counting straight off the table (rows written by the
# direct INSERTs in profiles.py / imported.py included)
TIER_SQL = (f"CASE WHEN severity = 'critical' OR kind IN ({_sql_list(ALWAYS_KINDS)}) "
            f"THEN 'critical' WHEN kind IN ({_sql_list(APPROVAL_KINDS)}) THEN 'approval' "
            "WHEN severity = 'info' THEN 'record' ELSE 'alert' END")


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
    d["count"] = d.get("count") or 1
    d["tier"] = tier(d.get("kind"), d.get("severity"))
    return d


async def notify_level(db: aiosqlite.Connection) -> str:
    from .db import get_state
    try:
        v = await get_state(db, NOTIFY_KEY)
    except Exception:                           # noqa: BLE001 — no table in a bare test
        v = None
    return v if v in LEVELS else DEFAULT_LEVEL


async def set_notify_level(db: aiosqlite.Connection, level: str) -> str:
    from .db import set_state
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    await set_state(db, NOTIFY_KEY, level)
    return level


# The two clocks, as functions so a replay of a real event log can drive them:
# the SQL one (last_seen and the coalescing window, the created_at format) and
# the rate limiter's.
def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_clock = time.monotonic

# per-key ping times: a kind's pings (non-critical) or one row's repeat pings
_pings: dict[str, deque] = {}


def _rate_ok(key: str, limit: int) -> bool:
    now = _clock()
    q = _pings.setdefault(key, deque())
    while q and now - q[0] > settings.security_ping_window_seconds:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    if len(_pings) > 2000:                      # repeat keys are per row: keep it bounded
        stale = [k for k, v in _pings.items()
                 if now - v[-1] > settings.security_ping_window_seconds]
        for k in stale:
            del _pings[k]
    return True


async def _coalesce_target(db: aiosqlite.Connection, kind: str, severity: str,
                           project: str | None, cause: str) -> dict | None:
    if settings.security_coalesce_seconds <= 0:
        return None
    async with db.execute(
            "SELECT id, count, summary FROM security_events WHERE kind = ? AND severity = ? "
            "AND project_slug IS ? AND cause = ? AND acknowledged = 0 "
            "AND COALESCE(last_seen, created_at) >= datetime(?, ?) "
            "ORDER BY id DESC LIMIT 1",
            (kind, severity, project, cause, _utcnow(),
             f"-{int(settings.security_coalesce_seconds)} seconds")) as cur:
        r = await cur.fetchone()
    return dict(r) if r else None


async def raise_event(db: aiosqlite.Connection, *, kind: str, summary: str,
                      severity: str = "warn", project: str | None = None,
                      detail: dict | None = None, cause: str | None = None) -> int:
    """Record one event (or count a repeat onto its unacknowledged twin) and
    publish it with the ping decision. `cause` is the coalescing key; the
    summary unless the raise site names something steadier. Returns the row
    id: a repeat returns the twin's."""
    cause = (cause or summary)[:500]
    t = tier(kind, severity)
    twin = await _coalesce_target(db, kind, severity, project, cause)
    if twin is not None:
        await db.execute("UPDATE security_events SET count = count + 1, "
                         "last_seen = ? WHERE id = ?", (_utcnow(), twin["id"]))
        await db.commit()
        # already in the queue: only a critical repeat interrupts again, and
        # at most once per ping window
        ping = t == "critical" and _rate_ok(f"#{twin['id']}", 1)
        bus.publish(SECURITY_CHAN, {"type": "security_event", "id": twin["id"],
                                    "kind": kind, "severity": severity, "project": project,
                                    "summary": twin["summary"], "detail": detail,
                                    "count": twin["count"] + 1, "repeat": True,
                                    "tier": t, "ping": ping})
        return twin["id"]
    cur = await db.execute(
        "INSERT INTO security_events(kind, severity, project_slug, summary, detail, "
        "cause, last_seen) VALUES (?,?,?,?,?,?,?)",
        (kind, severity, project, summary,
         json.dumps(detail) if detail is not None else None, cause, _utcnow()))
    await db.commit()
    level = await notify_level(db)
    ping = wants(t, level) and (t == "critical"
                                or _rate_ok(kind, settings.security_ping_per_kind))
    if t == "critical":
        _rate_ok(f"#{cur.lastrowid}", 1)        # its repeats stay quiet for a window
    # mirror the REST row shape (detail as an object) so live-SSE rows in the
    # Review Center render the same as poll-loaded ones
    bus.publish(SECURITY_CHAN, {"type": "security_event", "id": cur.lastrowid,
                                "kind": kind, "severity": severity, "project": project,
                                "summary": summary, "detail": detail,
                                "count": 1, "repeat": False, "tier": t, "ping": ping})
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


async def count_by_tier(db: aiosqlite.Connection) -> dict[str, int]:
    """Unacknowledged rows per tier: {critical, approval, alert, record}."""
    out = {"critical": 0, "approval": 0, "alert": 0, "record": 0}
    async with db.execute(
            f"SELECT {TIER_SQL} AS t, COUNT(*) AS n FROM security_events "
            "WHERE acknowledged = 0 GROUP BY t") as cur:
        for r in await cur.fetchall():
            out[r["t"]] = r["n"]
    return out


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
