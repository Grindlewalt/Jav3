"""Operator control surface for the git gate: status/diff are read-only views,
and approve/reject decide the agent's pending commit requests. The commit (and
push) only ever happens here — never from a tool call."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

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


class RemoteBody(BaseModel):
    url: str | None = None


@router.get("/remote")
async def get_remote(slug: str, fetch: bool = False):
    """The connected remote + ahead/behind vs origin. fetch=1 refreshes from
    the network first (slow; the panel calls it only on explicit sync)."""
    _check_project(slug)
    url = await gitgate.get_remote(slug)
    out = {"url": url, "has_token": gitgate.github_token() is not None}
    if url:
        try:
            ab = await gitgate.ahead_behind(slug, fetch=fetch)
            if ab:
                out.update(ab)
        except RuntimeError as e:
            out["error"] = str(e)
    return out


@router.put("/remote")
async def set_remote(slug: str, body: RemoteBody):
    """Connect (or disconnect with url=null) a GitHub remote. Verifies
    reachability/auth via ls-remote before saving."""
    _check_project(slug)
    url = (body.url or "").strip() or None
    if url:
        if not gitgate.valid_remote(url):
            raise HTTPException(
                status_code=400,
                detail="remote must look like https://github.com/<owner>/<repo>")
        try:
            await gitgate.verify_remote(slug, url)
        except RuntimeError as e:
            raise HTTPException(status_code=400,
                                detail=f"can't reach that repo: {e}")
    await gitgate.set_remote(slug, url)
    return {"url": url}


@router.post("/push")
async def push(slug: str):
    _check_project(slug)
    try:
        return {"output": await gitgate.push_to_remote(slug)}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/pull")
async def pull(slug: str):
    _check_project(slug)
    try:
        return {"output": await gitgate.pull_from_remote(slug)}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/requests")
async def requests(slug: str):
    _check_project(slug)
    return {"requests": await gitgate.list_requests(slug)}


@router.post("/requests/{rid}/approve")
async def approve(slug: str, rid: int):
    _check_project(slug)
    try:
        return await gitgate.approve_request(rid)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
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
