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
changes a severity or approves anything: quieter, same record.

Three more things the operator sets (Settings -> Notifications), all kept in
session_state next to the level:

  * Things I did myself: record only (`self_quiet`, on by default). A row raised
    with actor="operator" is filed already acknowledged and tagged "by you":
    no ping, not in the badge. The mark is EXPLICIT: an operator-facing route
    passes `actor="operator"`. It is never read from request or task context:
    a chat turn is started by the operator's HTTP request and its task inherits
    that context, so an agent's tool call would look like the operator's click.
    Critical rows are never quieted this way.
  * A mode per kind (`kinds`): ping | badge | record. A kind with no choice
    follows the level and its tier, which is what happened before this existed
    (DEFAULT_KIND_MODES names the exceptions). Record = filed acknowledged, kept
    in history; badge = counts in Security until acknowledged, never pings.
    Critical rows ignore it.
  * Do not disturb (`notify_dnd`): while on, nothing pings (critical only when
    `dnd_break_critical`, on by default). Badges still count and everything is
    still recorded; when it ends, one `dnd_summary` goes out. Approvals that
    hold a turn open (ask_user, permission asks, egress hosts) are not touched:
    the turn still waits and the item stays in the queue, only its ping is quiet.

Normal work, by rule (not the operator's setting: the code's). A raise site, or
the raise path itself, may judge an event routine: a scratch file the run itself
made deleted again, an import the project already uses, a known device's
connect/disconnect, a process the agent's own run_code started. `raise_event(
rule="why")` still RECORDS it, filed already acknowledged with quiet='rule' and
the reason in `rule`: no ping, not in the badge, not in the Queue, found in the
History under "filtered as normal work". Nothing is dropped. A critical row
ignores a rule (an anomaly cut is never routine). `_judge` holds the rules that
need the log itself (sessions per device per day, a standing fact once per box
allocation); the rest are judged where the event is raised.
"""
import asyncio
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import aiosqlite

from . import bus
from .config import settings

SECURITY_CHAN = "security"       # bus channel the bell + Review Center subscribe to

NOTIFY_KEY = "notify_level"      # session_state: the operator's ping level
PREFS_KEY = "notify_prefs"       # session_state JSON: {self_quiet, dnd_break_critical, kinds}
DND_KEY = "notify_dnd"           # session_state JSON: {since, until, from_id} while on
LEVELS = ("critical", "approvals", "all")
DEFAULT_LEVEL = "approvals"
MODES = ("ping", "badge", "record")
OPERATOR = "operator"            # the one actor value that means "their own click"
DND_MAX_DAYS = 14
# what a kind does until the operator picks: nothing, except the kinds named
# here. docker_weak_isolation is a standing note on every Docker box start, not
# an event. provider_balance is no security event either, but "the model's
# account is empty" must still reach someone who is not looking: it pings
# (warn, not critical) until they say otherwise.
DEFAULT_KIND_MODES = {"docker_weak_isolation": "record", "provider_balance": "ping"}
# every kind the code raises, with the severity it usually carries (the table in
# Settings -> Notifications lists these; a kind in the log but not here is
# added when it is read). A kind whose usual severity is critical is locked.
KNOWN_KINDS = {
    "login_failed": "warn", "write_flag": "warn", "plan_paused": "warn",
    "backup_config_changed": "warn", "mcp_unpinned_tools": "warn",
    "skill_import_flag": "warn", "skill_pin_mismatch": "critical",
    "provider_balance": "warn", "harness_fault": "info",
    "profile_changed": "warn", "profiles_migrated": "info",
    "lan_access_changed": "warn", "placement_changed": "info", "box_joined": "warn",
    "persist_approved": "warn", "persist_revoked": "info", "persist_imported": "warn",
    "persist_disk_deleted": "info", "persist_unplug_failed": "warn",
    "service_requested": "info", "service_approved": "warn", "service_rejected": "info",
    "service_revoked": "warn", "svc_unreported": "warn", "box_cap_refused": "info",
    "package_requested": "info", "package_approved": "warn", "package_rejected": "warn",
    "image_variant_built": "info", "gateway_cap": "warn",
    "docker_weak_isolation": "warn", "docker_isolation_note": "info",
    "docker_hardening_refused": "warn", "docker_socket_refused": "warn",
    "unexpected_process": "warn", "proc_report_mismatch": "warn",
    "proc_baseline_changed": "info",
    "egress_anomaly": "critical", "host_cut": "critical", "secret_leak": "critical",
    "egress_auto": "info",
    "always_loaded_changed": "info", "always_loaded_refused": "warn",
    "always_loaded_held": "warn", "always_loaded_approved": "info",
    "always_loaded_rejected": "info",
    "memory_deleted": "info", "memory_approved": "info", "memory_refused": "warn",
    "memory_proposed": "warn", "journal_unverified": "info",
    "desk_session": "info", "desk_killed": "warn", "desk_shell": "info",
    "desk_refused": "warn", "desk_shell_refused": "warn",
    "browser_session": "info", "browser_killed": "warn", "browser_refused": "warn",
    "browser_paused": "warn", "browser_resumed": "info", "browser_blind": "warn",
    "browser_cancelled": "warn", "browser_site_allowed": "info",
    "browser_site_denied": "info", "browser_popup_adopted": "info",
    "browser_rate_limited": "warn", "browser_paused_refusal": "warn",
    "device_enrolled": "info", "local_session": "info", "host_run": "info",
    "grounding_probe": "info",
}
# requests that wait on an operator decision in their own queue (services,
# packages): they ping at the default level whatever severity they carry
APPROVAL_KINDS = frozenset({"service_requested", "package_requested"})
# ping at every level even when raised below critical: an anomaly cut, a
# refused secret leak, a guest connection no reported process owns
ALWAYS_KINDS = frozenset({"egress_anomaly", "host_cut", "secret_leak",
                          "proc_report_mismatch"})

_COLUMNS = ("id, kind, severity, project_slug, summary, detail, acknowledged, "
            "created_at, acknowledged_at, triage_verdict, triage_reason, "
            "count, last_seen, cause, actor, quiet, rule")


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


# --- what the operator chose: per-kind modes, "my own actions", do not disturb --

def _parse_json(raw) -> dict:
    try:
        v = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _clean_prefs(raw: dict) -> dict:
    """The stored prefs with anything unreadable dropped: a bad blob must not
    turn pings off or on by accident, it reads as the defaults."""
    kinds = raw.get("kinds")
    return {"self_quiet": raw.get("self_quiet") is not False,
            "dnd_break_critical": raw.get("dnd_break_critical") is not False,
            "kinds": {k: m for k, m in (kinds.items() if isinstance(kinds, dict) else ())
                      if isinstance(k, str) and m in MODES}}


async def get_prefs(db: aiosqlite.Connection) -> dict:
    from .db import get_state
    try:
        raw = await get_state(db, PREFS_KEY)
    except Exception:                           # noqa: BLE001 — no table in a bare test
        raw = None
    return _clean_prefs(_parse_json(raw))


def locked(kind: str) -> bool:
    """A kind that always pings: it cannot be set to anything else."""
    return kind in ALWAYS_KINDS or KNOWN_KINDS.get(kind) == "critical"


async def known_kinds(db: aiosqlite.Connection) -> dict[str, str]:
    """KNOWN_KINDS plus any kind already in the log (with a severity it carried),
    so a kind this file does not list yet can still be set."""
    out = dict(KNOWN_KINDS)
    try:
        async with db.execute("SELECT kind, severity FROM security_events WHERE id IN "
                              "(SELECT MAX(id) FROM security_events GROUP BY kind)") as cur:
            for r in await cur.fetchall():
                out.setdefault(r["kind"], r["severity"] or "warn")
    except Exception:                           # noqa: BLE001
        pass
    return out


async def set_prefs(db: aiosqlite.Connection, *, self_quiet: bool | None = None,
                    dnd_break_critical: bool | None = None,
                    kinds: dict | None = None) -> dict:
    """Merge a change into the prefs. `kinds` maps kind -> ping|badge|record, or
    None to drop that kind's choice. Kinds and modes are checked here; a locked
    kind (critical: always pings) is refused. Raises ValueError."""
    from .db import set_state
    cur = await get_prefs(db)
    if self_quiet is not None:
        cur["self_quiet"] = bool(self_quiet)
    if dnd_break_critical is not None:
        cur["dnd_break_critical"] = bool(dnd_break_critical)
    if kinds:
        known = await known_kinds(db)
        for k, m in kinds.items():
            if k not in known:
                raise ValueError(f"unknown kind {k!r}")
            if locked(k):
                raise ValueError(f"{k} is critical: it always pings")
            if m is None:
                cur["kinds"].pop(k, None)
            elif m in MODES:
                cur["kinds"][k] = m
            else:
                raise ValueError(f"mode for {k} must be one of {', '.join(MODES)}")
    await set_state(db, PREFS_KEY, json.dumps(cur))
    return cur


def mode_for(kind: str, severity: str | None, level: str, prefs: dict) -> tuple[str, bool]:
    """(ping | badge | record, chosen). `chosen` is true when the mode is the
    operator's pick or a named default: only then is a record-mode row filed
    acknowledged. Without a pick, a kind follows the level and its tier, as it
    always did (an info row is a record that stays unacknowledged and out of
    the badge). Critical rows always ping."""
    t = tier(kind, severity)
    if t == "critical":
        return "ping", False
    pick = prefs["kinds"].get(kind) or DEFAULT_KIND_MODES.get(kind)
    if pick:
        return pick, True
    if wants(t, level):
        return "ping", False
    return ("record" if t == "record" else "badge"), False


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_when(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _dnd_view(state: dict, prefs: dict) -> dict:
    return {"on": bool(state), "since": state.get("since"), "until": state.get("until"),
            "break_critical": prefs["dnd_break_critical"]}


async def dnd_status(db: aiosqlite.Connection) -> dict:
    """{on, since, until, break_critical}. An expired DND ends HERE, whoever
    asks first (the poll, a new event, the timer): the end is one atomic delete,
    so the summary goes out once."""
    from .db import get_state
    prefs = await get_prefs(db)
    try:
        raw = await get_state(db, DND_KEY)
    except Exception:                           # noqa: BLE001
        raw = None
    state = _parse_json(raw)
    if not state:
        return _dnd_view({}, prefs)
    until = _parse_when(state.get("until")) if state.get("until") else None
    if until is not None and until <= datetime.now(timezone.utc):
        await _end_dnd(db, raw, state, prefs)
        return _dnd_view({}, prefs)
    if until is not None and _dnd_timer is None:
        _arm_dnd_timer(until)                   # after a restart: the first look re-arms it
    return _dnd_view(state, prefs)


async def set_dnd(db: aiosqlite.Connection, on: bool, *, until: str | None = None) -> dict:
    """Turn do not disturb on (optionally until an instant, ISO 8601) or off.
    Turning it off while on ends it like an expiry does: one summary. Raises
    ValueError for an end that is not in the future or is too far off."""
    from .db import get_state, set_state
    prefs = await get_prefs(db)
    raw = await get_state(db, DND_KEY)
    cur = _parse_json(raw)
    if not on:
        if cur:
            await _end_dnd(db, raw, cur, prefs)
        return _dnd_view({}, prefs)
    end = None
    if until:
        end = _parse_when(until)
        now = datetime.now(timezone.utc)
        if end is None:
            raise ValueError("until must be an ISO 8601 time")
        if end <= now:
            raise ValueError("until must be in the future")
        if end > now + timedelta(days=DND_MAX_DAYS):
            raise ValueError(f"until must be within {DND_MAX_DAYS} days")
    async with db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM security_events") as c:
        top = (await c.fetchone())["m"]
    # turning it on again keeps its start: the summary covers the whole stretch
    state = {"since": cur.get("since") or _iso(datetime.now(timezone.utc)),
             "until": _iso(end) if end else None,
             "from_id": cur.get("from_id", top)}
    await set_state(db, DND_KEY, json.dumps(state))
    if end:
        _arm_dnd_timer(end)
    elif _dnd_timer is not None:
        _dnd_timer.cancel()
    view = _dnd_view(state, prefs)
    bus.publish(SECURITY_CHAN, {"type": "dnd_changed", **view})
    return view


async def _end_dnd(db: aiosqlite.Connection, raw, state: dict, prefs: dict) -> None:
    """Clear DND (once: whoever deletes the row it was read as wins) and say how
    the stretch went: alerts that would have pinged and are still unacknowledged,
    and approvals waiting now."""
    cur = await db.execute("DELETE FROM session_state WHERE key = ? AND value = ?",
                           (DND_KEY, raw))
    await db.commit()
    if _dnd_timer is not None:
        _dnd_timer.cancel()
    if cur.rowcount == 0:
        return                                  # someone else ended it
    level = await notify_level(db)
    held = 0
    async with db.execute(
            "SELECT kind, severity, COUNT(*) AS n FROM security_events "
            "WHERE acknowledged = 0 AND id > ? GROUP BY kind, severity",
            (int(state.get("from_id") or 0),)) as c:
        for r in await c.fetchall():
            mode, _ = mode_for(r["kind"], r["severity"], level, prefs)
            if mode == "ping" and not (tier(r["kind"], r["severity"]) == "critical"
                                       and prefs["dnd_break_critical"]):
                held += r["n"]
    try:
        from . import notifications_api
        approvals = await notifications_api.approvals_waiting()
    except Exception:                           # noqa: BLE001 — the summary is best effort
        approvals = 0
    bus.publish(SECURITY_CHAN, {"type": "dnd_changed", **_dnd_view({}, prefs)})
    if held or approvals:
        bits = [f"{held} alert{'' if held == 1 else 's'}",
                f"{approvals} approval{'' if approvals == 1 else 's'} waiting"]
        bus.publish(SECURITY_CHAN, {
            "type": "dnd_summary", "alerts": held, "approvals": approvals, "ping": True,
            "summary": "While you were in do-not-disturb: " + ", ".join(bits)})


# the timer that ends a timed DND on the dot; the lazy check in dnd_status is
# the backstop (a restart loses the timer)
_dnd_timer: asyncio.TimerHandle | None = None


def _arm_dnd_timer(until: datetime) -> None:
    global _dnd_timer
    if _dnd_timer is not None:
        _dnd_timer.cancel()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    delay = max(0.0, (until - datetime.now(timezone.utc)).total_seconds()) + 0.5
    _dnd_timer = loop.call_later(delay, lambda: asyncio.ensure_future(_dnd_expired()))


async def _dnd_expired() -> None:
    try:
        from .db import get_db
        db = await get_db()
        try:
            await dnd_status(db)
        finally:
            await db.close()
    except Exception:                           # noqa: BLE001 — the next poll ends it
        pass


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
                           project: str | None, cause: str, *,
                           actor: str | None = None, quiet: str | None = None) -> dict | None:
    """The row a repeat counts onto: the same event, from the same actor, in the
    same state (waiting in the queue, or filed quietly for a Record-only kind)."""
    if settings.security_coalesce_seconds <= 0:
        return None
    async with db.execute(
            "SELECT id, count, summary FROM security_events WHERE kind = ? AND severity = ? "
            "AND project_slug IS ? AND cause = ? AND acknowledged = ? "
            "AND COALESCE(quiet, '') = ? AND COALESCE(actor, '') = ? "
            "AND COALESCE(last_seen, created_at) >= datetime(?, ?) "
            "ORDER BY id DESC LIMIT 1",
            (kind, severity, project, cause, 1 if quiet else 0, quiet or "", actor or "",
             _utcnow(), f"-{int(settings.security_coalesce_seconds)} seconds")) as cur:
        r = await cur.fetchone()
    return dict(r) if r else None


def _decide(kind: str, severity: str, actor: str | None, level: str, prefs: dict,
            dnd_on: bool, rule: str | None = None) -> dict:
    """Everything the operator's settings say about one new event: its tier,
    whether it is filed already acknowledged (`quiet`: 'operator', 'rule' or
    'kind'), whether it pings, and whether do-not-disturb held a ping back. A
    rule's verdict ("normal work") beats a kind's mode: the operator who set a
    kind to Ping wants its real events, not the routine ones."""
    t = tier(kind, severity)
    mode, chosen = mode_for(kind, severity, level, prefs)
    quiet = None
    if t != "critical":
        if actor == OPERATOR and prefs["self_quiet"]:
            quiet = "operator"
        elif rule:
            quiet = "rule"
        elif chosen and mode == "record":
            quiet = "kind"
    ping = quiet is None and mode == "ping"
    breaks = bool(dnd_on and t == "critical" and prefs["dnd_break_critical"])
    held = bool(ping and dnd_on and not breaks)
    return {"tier": t, "quiet": quiet, "mode": mode, "held": held,
            "ping": ping and not held, "breaks": breaks}


async def decide(db: aiosqlite.Connection, kind: str, severity: str,
                 actor: str | None, rule: str | None = None) -> dict:
    try:
        dnd_on = (await dnd_status(db))["on"]
    except Exception:                           # noqa: BLE001 — fail toward pinging, not silence
        dnd_on = False
    return _decide(kind, severity, actor, await notify_level(db), await get_prefs(db), dnd_on,
                   rule)


def decide_sync(con, kind: str, severity: str, actor: str | None = None) -> dict:
    """decide() for a caller with a plain sqlite3 connection and no event loop
    (imported.alert). An expired DND reads as off; the async side ends it."""
    def get(key):
        try:
            r = con.execute("SELECT value FROM session_state WHERE key = ?", (key,)).fetchone()
        except Exception:                       # noqa: BLE001 — no table yet
            return None
        return r[0] if r else None
    level = get(NOTIFY_KEY)
    state = _parse_json(get(DND_KEY))
    until = _parse_when(state.get("until")) if state.get("until") else None
    on = bool(state) and (until is None or until > datetime.now(timezone.utc))
    return _decide(kind, severity, actor, level if level in LEVELS else DEFAULT_LEVEL,
                   _clean_prefs(_parse_json(get(PREFS_KEY))), on)


# --- rules that need the log itself ------------------------------------------
# Judged by kind in the raise path, so the raise sites (browser.py, desk.py,
# docker_runtime.py) stay as they are.

SESSION_KINDS = frozenset({"browser_session", "desk_session"})
SESSION_RULE = "known device: its connects and disconnects are filed once a day"
# a standing fact is true until the thing it describes is rebuilt: once per
# allocation of the box, however often the box starts
STANDING_KINDS = frozenset({"docker_weak_isolation"})


def _live_turn() -> bool:
    """Is any agent loop running right now? Unknown reads as yes: an error here
    must make an event more visible, never less."""
    try:
        from . import chat
        return bool(chat._running_loops())
    except Exception:                           # noqa: BLE001
        return True


async def _device_seen(db: aiosqlite.Connection, kind: str, device) -> bool:
    try:
        async with db.execute(
                "SELECT 1 FROM security_events WHERE kind = ? "
                "AND json_extract(detail, '$.device_id') = ? LIMIT 1",
                (kind, device)) as cur:
            return await cur.fetchone() is not None
    except Exception:                           # noqa: BLE001 — no JSON1: raise it
        return False


async def _judge(db: aiosqlite.Connection, kind: str, detail: dict | None) -> dict | None:
    """The rule for a kind that is judged here, if any: {"rule": reason,
    "cause": key} files it quiet onto a steadier row, {"cause": key} alone names
    the row of its own, {"standing": key} counts it onto the row that already
    states the fact, None leaves it alone.

    browser_session / desk_session (a device connected or disconnected): one row
    per device per day. Raised in full only for a device this log has never
    seen, or one that went silent while a turn was running (that turn may be
    stuck on it). The other 280 a week are the same browser reconnecting.
    docker_weak_isolation: the box's isolation is a fact about its allocation,
    not an event of each start."""
    d = detail if isinstance(detail, dict) else {}
    if kind in SESSION_KINDS:
        device = d.get("device_id")
        if device is None:
            return None
        if not await _device_seen(db, kind, device):
            return {"cause": f"{kind}:{device}:first"}
        if d.get("why") == "went silent" and _live_turn():
            return None
        return {"rule": SESSION_RULE, "cause": f"{kind}:{device}:{_utcnow()[:10]}"}
    if kind in STANDING_KINDS:
        box = d.get("box")
        if not box:
            return None
        return {"standing": f"{kind}:{box}@{d.get('allocation') or _utcnow()[:10]}"}
    return None


async def _count_repeat(db: aiosqlite.Connection, twin: dict, *, kind: str, severity: str,
                        project: str | None, detail: dict | None, t: str, ping: bool,
                        quiet: str | None, actor: str | None) -> int:
    """Count a repeat onto the row that already says it."""
    await db.execute("UPDATE security_events SET count = count + 1, "
                     "last_seen = ? WHERE id = ?", (_utcnow(), twin["id"]))
    await db.commit()
    bus.publish(SECURITY_CHAN, {"type": "security_event", "id": twin["id"],
                                "kind": kind, "severity": severity, "project": project,
                                "summary": twin["summary"], "detail": detail,
                                "count": twin["count"] + 1, "repeat": True,
                                "tier": t, "ping": ping, "acknowledged": bool(quiet),
                                "actor": actor})
    return twin["id"]


async def raise_event(db: aiosqlite.Connection, *, kind: str, summary: str,
                      severity: str = "warn", project: str | None = None,
                      detail: dict | None = None, cause: str | None = None,
                      actor: str | None = None, rule: str | None = None) -> int:
    """Record one event (or count a repeat onto its twin) and publish it with
    the ping decision. `cause` is the coalescing key; the summary unless the
    raise site names something steadier. Returns the row id: a repeat returns
    the twin's.

    `actor="operator"` says the operator's own click caused it. Only an
    operator-facing route may pass it (see the module doc); nothing is inferred.

    `rule` is a short reason the raise site judged this event normal work (see
    the module doc): it is filed acknowledged, quiet='rule', no ping. Ignored
    for a critical event.
    """
    cause = (cause or summary)[:500]
    actor = OPERATOR if actor == OPERATOR else None
    rule = (rule or "").strip()[:200] or None
    j = await _judge(db, kind, detail)
    if j and j.get("standing"):
        cause = j["standing"][:500]
        async with db.execute("SELECT id, count, summary FROM security_events "
                              "WHERE kind = ? AND cause = ? ORDER BY id DESC LIMIT 1",
                              (kind, cause)) as cur:
            row = await cur.fetchone()
        if row is not None:
            d = await decide(db, kind, severity, actor)
            return await _count_repeat(db, dict(row), kind=kind, severity=severity,
                                       project=project, detail=detail, t=d["tier"],
                                       ping=False, quiet=d["quiet"], actor=actor)
    elif j:
        rule, cause = rule or j.get("rule"), j["cause"][:500]
    d = await decide(db, kind, severity, actor, rule)
    t, quiet = d["tier"], d["quiet"]
    # their own action is one row each time; a Record-only kind's repeats pile
    # onto one quiet row; everything else onto its waiting twin
    twin = (None if quiet == "operator"
            else await _coalesce_target(db, kind, severity, project, cause,
                                        actor=actor, quiet=quiet))
    if twin is not None:
        # already in the queue: only a critical repeat interrupts again, and
        # at most once per ping window
        ping = (t == "critical" and (not d["held"] or d["breaks"])
                and _rate_ok(f"#{twin['id']}", 1))
        return await _count_repeat(db, twin, kind=kind, severity=severity, project=project,
                                   detail=detail, t=t, ping=ping, quiet=quiet, actor=actor)
    ping = d["ping"] and (t == "critical" or _rate_ok(kind, settings.security_ping_per_kind))
    cur = await db.execute(
        "INSERT INTO security_events(kind, severity, project_slug, summary, detail, "
        "cause, last_seen, actor, quiet, rule, acknowledged, acknowledged_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, severity, project, summary,
         json.dumps(detail) if detail is not None else None, cause, _utcnow(),
         actor, quiet, rule if quiet == "rule" else None,
         1 if quiet else 0, _utcnow() if quiet else None))
    await db.commit()
    if t == "critical":
        _rate_ok(f"#{cur.lastrowid}", 1)        # its repeats stay quiet for a window
    # mirror the REST row shape (detail as an object) so live-SSE rows in the
    # Review Center render the same as poll-loaded ones
    bus.publish(SECURITY_CHAN, {"type": "security_event", "id": cur.lastrowid,
                                "kind": kind, "severity": severity, "project": project,
                                "summary": summary, "detail": detail,
                                "count": 1, "repeat": False, "tier": t, "ping": ping,
                                "acknowledged": bool(quiet), "actor": actor,
                                "quiet": quiet, "rule": rule if quiet == "rule" else None})
    return cur.lastrowid


def raise_sync(con, *, kind: str, summary: str, severity: str = "warn",
               detail: dict | None = None) -> int | None:
    """raise_event for a caller with a plain sqlite3 connection and no event loop
    (imported.alert, run while the skills registry compiles). No coalescing: its
    caller dedupes. Same decision, same stamped event. None if there is no table."""
    import sqlite3
    d = decide_sync(con, kind, severity)
    quiet = d["quiet"]
    blob = json.dumps(detail) if detail else None
    try:
        try:
            cur = con.execute(
                "INSERT INTO security_events(kind, severity, summary, detail, last_seen, "
                "quiet, acknowledged, acknowledged_at) VALUES (?,?,?,?,?,?,?,?)",
                (kind, severity, summary, blob, _utcnow(), quiet, 1 if quiet else 0,
                 _utcnow() if quiet else None))
        except sqlite3.OperationalError:        # a database from before the quiet column
            cur = con.execute("INSERT INTO security_events(kind, severity, summary, detail) "
                              "VALUES (?,?,?,?)", (kind, severity, summary, blob))
        con.commit()
    except sqlite3.Error:
        return None
    ping = d["ping"] and (d["tier"] == "critical"
                          or _rate_ok(kind, settings.security_ping_per_kind))
    if d["tier"] == "critical":
        _rate_ok(f"#{cur.lastrowid}", 1)
    bus.publish(SECURITY_CHAN, {"type": "security_event", "id": cur.lastrowid, "kind": kind,
                                "severity": severity, "project": None, "summary": summary,
                                "detail": detail, "count": 1, "repeat": False,
                                "tier": d["tier"], "ping": ping, "acknowledged": bool(quiet),
                                "actor": None})
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


async def acknowledge_all(db: aiosqlite.Connection, *, only: str | None = None,
                          exclude: str | None = None) -> dict:
    """Operator bulk-clear — the queue reached hundreds in practice. `only` /
    `exclude` name one kind: the Queue keeps agent reports (harness_fault) in
    a list of their own, cleared apart from the alerts."""
    where, args = "acknowledged=0", []
    if only:
        where += " AND kind = ?"
        args.append(only)
    if exclude:
        where += " AND kind != ?"
        args.append(exclude)
    cur = await db.execute("UPDATE security_events SET acknowledged=1, "
                           f"acknowledged_at=datetime('now') WHERE {where}", args)
    await db.commit()
    return {"ok": True, "done": cur.rowcount}


async def count_unacknowledged(db: aiosqlite.Connection) -> int:
    async with db.execute(
            "SELECT COUNT(*) AS n FROM security_events WHERE acknowledged = 0") as cur:
        return (await cur.fetchone())["n"]


async def tier_counts(db: aiosqlite.Connection) -> tuple[dict[str, int], dict[str, int]]:
    """Unacknowledged rows per tier ({critical, approval, alert, record}), as the
    operator's per-kind choices see them, and how many of each would ping.
    A kind set to Record that is still waiting (it was chosen after the row
    came in) counts as a record; an info kind set to Badge or Ping counts as an
    alert. Critical rows are always critical and always ping."""
    level, prefs = await notify_level(db), await get_prefs(db)
    out = {"critical": 0, "approval": 0, "alert": 0, "record": 0}
    pinging = {"critical": 0, "approval": 0, "alert": 0, "record": 0}
    async with db.execute(
            "SELECT kind, severity, COUNT(*) AS n FROM security_events "
            "WHERE acknowledged = 0 GROUP BY kind, severity") as cur:
        for r in await cur.fetchall():
            t = tier(r["kind"], r["severity"])
            mode, _ = mode_for(r["kind"], r["severity"], level, prefs)
            if t != "critical":
                t = "record" if mode == "record" else t if t == "approval" else "alert"
            out[t] += r["n"]
            if mode == "ping":
                pinging[t] += r["n"]
    return out, pinging


async def count_by_tier(db: aiosqlite.Connection) -> dict[str, int]:
    """Unacknowledged rows per tier: {critical, approval, alert, record}."""
    return (await tier_counts(db))[0]


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
    if len(head) > 160:                 # the whole text is in the detail (the board shows it)
        head = head[:159].rstrip() + "…"
    await raise_event(
        db, kind="harness_fault", severity="info",
        project=project, summary=f"Harness fault reported: {head}",
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
