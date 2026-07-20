"""Approval notification center — one place that answers "what is Jarvis waiting
on me for?" Aggregates the independent pending stores that otherwise each
live behind their own page (or, for git, behind no page at all):

  - git push requests awaiting approval   (git_requests table)
  - staged changes awaiting review        (.staging/ per project)
  - schedules Jarvis proposed             (schedules with pending_approval = 1)

Read-only aggregation — it never approves anything, just surfaces a count + list
so the nav can show a badge. Each source is wrapped so one failing store does not
blank the whole panel."""
from fastapi import APIRouter, Depends

from . import egress, gitgate, security, staging
from .auth import require_user
from .db import get_db
from .projects import list_projects

router = APIRouter(prefix="/api/notifications", tags=["notifications"],
                   dependencies=[Depends(require_user)])


async def _git_pending(slugs: list[str]) -> list[dict]:
    out = []
    for s in slugs:
        try:
            for r in await gitgate.list_requests(s):
                if r.get("status") == "pending":
                    out.append({"project": s, "id": r["id"],
                                "message": r.get("message", ""),
                                "created_at": r.get("created_at")})
        except Exception:                       # noqa: BLE001 (one project's failure is not fatal)
            continue
    return out


def _staged_pending(slugs: list[str]) -> list[dict]:
    out = []
    for s in slugs:
        try:
            files = staging.list_staged(s)
        except Exception:                       # noqa: BLE001
            continue
        if files:
            out.append({"project": s, "files": len(files)})
    return out


async def _schedules_pending() -> list[dict]:
    """Schedules Jarvis proposed via schedule_update: paused until the
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
    out = {"alerts": 0, "egress_pending": 0}
    try:
        db = await get_db()
        try:
            out["alerts"] = await security.count_unacknowledged(db)
            out["egress_pending"] = len(await egress.list_pending(db))
        finally:
            await db.close()
    except Exception:                           # noqa: BLE001
        pass
    return out


@router.get("")
async def notifications():
    try:
        proj = (await list_projects()).get("projects", [])
    except Exception:                           # noqa: BLE001
        proj = []
    slugs = [p["slug"] for p in proj]
    git = await _git_pending(slugs)
    staged = _staged_pending(slugs)
    sched = await _schedules_pending()
    sec = await _security_pending()
    return {
        "count": (len(git) + len(staged) + len(sched)
                  + sec["alerts"] + sec["egress_pending"]),
        "git": git, "staged": staged, "schedules": sched,
        "alerts": sec["alerts"], "egress_pending": sec["egress_pending"],
    }
