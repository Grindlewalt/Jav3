"""Operator control surface for the git gate: status/diff are read-only views,
and approve/reject decide the agent's pending commit requests. The commit (and
push) only ever happens here — never from a tool call."""
from fastapi import APIRouter, Depends, HTTPException

from . import gitgate
from .auth import require_user
from .config import settings

router = APIRouter(prefix="/api/projects/{slug}/git", tags=["git"],
                   dependencies=[Depends(require_user)])


def _check_project(slug: str) -> None:
    d = settings.projects_dir / slug
    if not d.is_dir() or not (d / "project.md").exists():
        raise HTTPException(status_code=404, detail="no such project")


@router.get("/status")
async def status(slug: str):
    _check_project(slug)
    await gitgate.ensure_repo(slug)
    return {"status": await gitgate.status_text(slug)}


@router.get("/diff")
async def diff(slug: str, path: str | None = None):
    _check_project(slug)
    await gitgate.ensure_repo(slug)
    return {"diff": await gitgate.diff_text(slug, path)}


@router.get("/requests")
async def requests(slug: str):
    _check_project(slug)
    return {"requests": await gitgate.list_requests(slug)}


@router.post("/requests/{rid}/approve")
async def approve(slug: str, rid: int, force: bool = False):
    _check_project(slug)
    try:
        return await gitgate.approve_request(rid, force=force)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        # anti-malware gate blocked the push; operator can retry with ?force=true
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/requests/{rid}/reject")
async def reject(slug: str, rid: int):
    _check_project(slug)
    try:
        return await gitgate.reject_request(rid)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
