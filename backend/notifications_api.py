"""Approval notification center — one place that answers "what is Jav3 waiting
on me for?" Aggregates the independent pending stores that otherwise each
live behind their own page (or, for git, behind no page at all):

  - git push requests awaiting approval   (git_requests table)
  - schedules Jav3 proposed             (schedules with pending_approval = 1)
  - shell commands a computer-use turn is waiting on (backend/desk.py)

Read-only aggregation — it never approves anything, just surfaces a count + list
so the nav can show a badge. Each source is wrapped so one failing store does not
blank the whole panel.

The badge counts what waits on the operator: approvals and every unacknowledged
security event except info records (an audit line is not a to-do; it stays in
the Security log). `ping_count` is the subset that would interrupt at the
operator's level (security.LEVELS) and per-kind modes, for the "N waiting" card
on page load; while do-not-disturb is on it is 0 (or just the critical alerts
that break through). `/settings` reads and sets the level, "things I did
myself: record only", the per-kind table and the critical-breaks-through
choice; `/dnd` turns do-not-disturb on and off."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import bus, security
from .auth import require_user
from .db import get_db
from .projects import list_projects

router = APIRouter(prefix="/api/notifications", tags=["notifications"],
                   dependencies=[Depends(require_user)])


async def _git_pending(slugs: list[str]) -> list[dict]:
    """One query on one connection — the old per-project gitgate.list_requests
    loop opened N sqlite connections per 15s bell poll, per open tab."""
    try:
        db = await get_db()
        try:
            async with db.execute(
                "SELECT id, project_slug, message, created_at FROM git_requests "
                "WHERE status = 'pending' ORDER BY id DESC") as cur:
                rows = await cur.fetchall()
        finally:
            await db.close()
        keep = set(slugs)
        return [{"project": r["project_slug"], "id": r["id"],
                 "message": r["message"], "created_at": r["created_at"]}
                for r in rows if r["project_slug"] in keep]
    except Exception:                           # noqa: BLE001
        return []


async def _schedules_pending() -> list[dict]:
    """Schedules Jav3 proposed via schedule_update: paused until the
    operator resumes (approve) or pauses (park) them in the GUI."""
    try:
        db = await get_db()
        try:
            async with db.execute(
                "SELECT id, name, task, kind, agent_slug FROM schedules "
                "WHERE pending_approval = 1 ORDER BY id DESC") as cur:
                return [dict(r) for r in await cur.fetchall()]
        finally:
            await db.close()
    except Exception:                           # noqa: BLE001
        return []


async def _security_pending() -> dict:
    """Unacknowledged security alerts + open egress host approvals — the
    monitored-egress / diff-gate signals for the bell and Review Center."""
    out = {"alerts": 0, "egress_pending": 0, "level": security.DEFAULT_LEVEL,
           "tiers": {"critical": 0, "approval": 0, "alert": 0, "record": 0},
           "pinging": {"critical": 0, "approval": 0, "alert": 0, "record": 0},
           "dnd": {"on": False, "since": None, "until": None, "break_critical": True}}
    try:
        db = await get_db()
        try:
            out["level"] = await security.notify_level(db)
            out["tiers"], out["pinging"] = await security.tier_counts(db)
            out["dnd"] = await security.dnd_status(db)
            t = out["tiers"]
            out["alerts"] = t["critical"] + t["approval"] + t["alert"]
            async with db.execute(
                "SELECT COUNT(*) AS n FROM egress_pending "
                "WHERE status = 'pending'") as cur:
                out["egress_pending"] = (await cur.fetchone())["n"]
        finally:
            await db.close()
    except Exception:                           # noqa: BLE001
        pass
    return out


async def _desk_pending() -> list[dict]:
    """Shell commands a turn is blocked on right now (answered in Settings →
    Computer use). Only the ones something is still waiting for."""
    try:
        from . import desk
        return [{"id": p["id"], "name": p["name"], "command": p["command"][:200]}
                for p in await desk.list_pending()]
    except Exception:                           # noqa: BLE001
        return []


async def _gather() -> dict:
    """Everything that waits on the operator, from every store."""
    try:
        proj = (await list_projects()).get("projects", [])
    except Exception:                           # noqa: BLE001
        proj = []
    slugs = [p["slug"] for p in proj]
    from . import operator_ask
    sec = await _security_pending()
    return {"git": await _git_pending(slugs), "sched": await _schedules_pending(),
            "sec": sec, "shell": await _desk_pending(),
            "asks": operator_ask.pending_list()}      # ask_user / permission asks


def _approvals(g: dict) -> int:
    """Items a person has to answer: git pushes, proposed schedules, shell asks,
    ask_user / permission asks, egress hosts and approval-tier security rows."""
    return (len(g["git"]) + len(g["sched"]) + len(g["shell"]) + len(g["asks"])
            + g["sec"]["egress_pending"] + g["sec"]["tiers"]["approval"])


async def approvals_waiting() -> int:
    """How many approvals wait right now (the do-not-disturb summary's number)."""
    return _approvals(await _gather())


@router.get("")
async def notifications():
    g = await _gather()
    git, sched, sec, shell, asks = g["git"], g["sched"], g["sec"], g["shell"], g["asks"]
    tiers, level, dnd = sec["tiers"], sec["level"], sec["dnd"]
    try:                # notes/changes awaiting approval: the Memory nav badge, kept
        from .memory import pending_counts     # out of `count` (Security's number)
        from . import alwaysloaded
        # ...plus writes to always-loaded project files held for approval
        memory_pending = pending_counts()["total"] + alwaysloaded.pending_total()
    except Exception:                           # noqa: BLE001
        memory_pending = 0
    approvals = _approvals(g)
    # what would interrupt: critical always; the rest by the level and the
    # per-kind modes (security rows) or the level alone (the other approvals).
    # Do not disturb silences all of it but a critical alert it lets through.
    # The approvals themselves stay in `count`: a turn may be waiting on one.
    pings = sec["pinging"]
    other = approvals - tiers["approval"]
    ping_count = (pings["critical"]
                  + (other if security.wants("approval", level) else 0)
                  + pings["approval"] + pings["alert"])
    if dnd["on"]:
        ping_count = pings["critical"] if dnd["break_critical"] else 0
    return {
        "count": approvals + tiers["critical"] + tiers["alert"],
        "git": git, "schedules": sched, "desk_shell": shell, "asks": asks,
        "memory_pending": memory_pending,
        "alerts": sec["alerts"], "egress_pending": sec["egress_pending"],
        "critical": tiers["critical"], "records": tiers["record"], "level": level,
        "ping_count": ping_count, "dnd": dnd,
    }


class SettingsBody(BaseModel):
    level: str | None = None
    self_quiet: bool | None = None
    dnd_break_critical: bool | None = None
    kinds: dict[str, str | None] | None = None      # kind -> ping | badge | record | null


class DndBody(BaseModel):
    on: bool = True
    until: str | None = None          # ISO 8601 instant; the client works out "tomorrow 08:00"
    minutes: int | None = None        # or: this long from now


async def _settings_view(db) -> dict:
    level, prefs = await security.notify_level(db), await security.get_prefs(db)
    kinds = []
    for kind, usual in (await security.known_kinds(db)).items():
        t = security.tier(kind, usual)
        mode, chosen = security.mode_for(kind, usual, level, prefs)
        # what it would do with no choice of the operator's
        base, _ = security.mode_for(kind, usual, level, {"kinds": {}})
        base = security.DEFAULT_KIND_MODES.get(kind, base)
        kinds.append({"kind": kind, "usual": usual, "tier": t,
                      "locked": security.locked(kind),
                      "mode": "ping" if security.locked(kind) else mode,
                      "default": "ping" if security.locked(kind) else base,
                      "chosen": kind in prefs["kinds"]})
    kinds.sort(key=lambda k: (k["locked"], k["kind"]))
    return {"level": level, "levels": list(security.LEVELS),
            "self_quiet": prefs["self_quiet"],
            "dnd_break_critical": prefs["dnd_break_critical"],
            "dnd": await security.dnd_status(db),
            "modes": list(security.MODES), "kinds": kinds}


@router.get("/settings")
async def get_settings():
    db = await get_db()
    try:
        return await _settings_view(db)
    finally:
        await db.close()


@router.put("/settings")
async def put_settings(body: SettingsBody):
    db = await get_db()
    try:
        try:
            if body.level is not None:
                await security.set_notify_level(db, body.level)
            await security.set_prefs(db, self_quiet=body.self_quiet,
                                     dnd_break_critical=body.dnd_break_critical,
                                     kinds=body.kinds)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await db.commit()
        view = await _settings_view(db)
        bus.publish(security.SECURITY_CHAN, {"type": "dnd_changed", **view["dnd"]})
        return view
    finally:
        await db.close()


@router.get("/dnd")
async def get_dnd():
    db = await get_db()
    try:
        return await security.dnd_status(db)
    finally:
        await db.close()


@router.put("/dnd")
async def put_dnd(body: DndBody):
    """Do not disturb on (until an instant, or for `minutes`, or until turned
    off) or off. Turning it off is what sends the one summary."""
    until = body.until
    if body.on and body.minutes is not None:
        if not 0 < body.minutes <= security.DND_MAX_DAYS * 1440:
            raise HTTPException(status_code=400, detail="minutes is out of range")
        until = security._iso(datetime.now(timezone.utc) + timedelta(minutes=body.minutes))
    db = await get_db()
    try:
        try:
            return await security.set_dnd(db, body.on, until=until)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        await db.close()
