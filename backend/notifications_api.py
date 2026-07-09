"""Approval notification center — one place that answers "what is Jarvis waiting
on me for?" Aggregates the three independent pending stores that otherwise each
live behind their own page (or, for git, behind no page at all):

  - git push requests awaiting approval   (git_requests table)
  - staged changes awaiting review        (.staging/ per project)
  - undecided sandbox sessions            (gated runs not yet approved/quarantined)
  - schedules Jarvis proposed             (schedules with pending_approval = 1)

Read-only aggregation — it never approves anything, just surfaces a count + list
so the nav can show a badge. Each source is wrapped so one failing store does not
blank the whole panel. Sandbox verdicts come from the same deterministic
classifier the console uses (never a model)."""
import json

from fastapi import APIRouter, Depends

from . import gitgate, sandbox, staging, threatintel
from .auth import require_user
from .config import settings
from .db import get_db
from .projects import list_projects

router = APIRouter(prefix="/api/notifications", tags=["notifications"],
                   dependencies=[Depends(require_user)])


def _evidence_path(run_id: int):
    return settings.vm_dir / "captures" / f"gate-{run_id}-evidence.json"


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


async def _sandbox_pending() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT r.id, p.slug FROM runs r JOIN projects p ON p.id = r.project_id "
            "WHERE r.status NOT IN ('approved', 'quarantined') "
            "ORDER BY r.id DESC LIMIT 40")
        rows = [dict(x) for x in await cur.fetchall()]
    finally:
        await db.close()
    idx = await sandbox.rules_index()
    bl = threatintel.load()
    out = []
    for r in rows:
        p = _evidence_path(r["id"])
        if not p.is_file():
            continue
        try:
            ev = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        c = sandbox.classify(ev, idx, bl)
        out.append({"run_id": r["id"], "project": r["slug"],
                    "verdict": c["verdict"], "headline": c["headline"]})
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


@router.get("")
async def notifications():
    try:
        proj = (await list_projects()).get("projects", [])
    except Exception:                           # noqa: BLE001
        proj = []
    slugs = [p["slug"] for p in proj]
    git = await _git_pending(slugs)
    staged = _staged_pending(slugs)
    sbx = await _sandbox_pending()
    sched = await _schedules_pending()
    return {
        "count": len(git) + len(staged) + len(sbx) + len(sched),
        "git": git, "staged": staged, "sandbox": sbx, "schedules": sched,
    }
