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
operator's level (security.LEVELS), for the "N waiting" card on page load.
`/settings` reads and sets that level."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import security
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
           "tiers": {"critical": 0, "approval": 0, "alert": 0, "record": 0}}
    try:
        db = await get_db()
        try:
            out["level"] = await security.notify_level(db)
            out["tiers"] = await security.count_by_tier(db)
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


@router.get("")
async def notifications():
    try:
        proj = (await list_projects()).get("projects", [])
    except Exception:                           # noqa: BLE001
        proj = []
    slugs = [p["slug"] for p in proj]
    git = await _git_pending(slugs)
    sched = await _schedules_pending()
    sec = await _security_pending()
    shell = await _desk_pending()
    from . import operator_ask
    asks = operator_ask.pending_list()      # ask_user / permission asks
    tiers, level = sec["tiers"], sec["level"]
    try:                # notes/changes awaiting approval: the Memory nav badge, kept
        from .memory import pending_counts     # out of `count` (Security's number)
        memory_pending = pending_counts()["total"]
    except Exception:                           # noqa: BLE001
        memory_pending = 0
    approvals = (len(git) + len(sched) + len(shell) + len(asks)
                 + sec["egress_pending"] + tiers["approval"])
    return {
        "count": approvals + tiers["critical"] + tiers["alert"],
        "git": git, "schedules": sched, "desk_shell": shell, "asks": asks,
        "memory_pending": memory_pending,
        "alerts": sec["alerts"], "egress_pending": sec["egress_pending"],
        "critical": tiers["critical"], "records": tiers["record"], "level": level,
        "ping_count": (tiers["critical"]
                       + (approvals if security.wants("approval", level) else 0)
                       + (tiers["alert"] if security.wants("alert", level) else 0)),
    }


class LevelBody(BaseModel):
    level: str


@router.get("/settings")
async def get_settings():
    db = await get_db()
    try:
        return {"level": await security.notify_level(db), "levels": list(security.LEVELS)}
    finally:
        await db.close()


@router.put("/settings")
async def put_settings(body: LevelBody):
    db = await get_db()
    try:
        try:
            level = await security.set_notify_level(db, body.level)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await db.commit()
        return {"level": level, "levels": list(security.LEVELS)}
    finally:
        await db.close()
