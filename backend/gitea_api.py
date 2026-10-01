"""The Git page (and the Gitea status the rest of the app asks for): the repo
list, one repo's branches, history, agent pull requests, push status and who
can open it, and the account basics (list, create, reset a password, disable).
Operator-only: require_user takes the password session cookie and nothing
else, so a device token (chat or cli) never reaches these. Every call goes to
Gitea's admin API host-side with the operator's token, which never leaves the
host. Approving or rejecting an agent pull request is not here: it is the
existing /api/projects/{slug}/git/requests/{id}/approve|reject."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import gitea
from .auth import require_user
from .config import settings

router = APIRouter(prefix="/api/gitea", tags=["gitea"],
                   dependencies=[Depends(require_user)])


def _need():
    if not gitea.enabled():
        raise HTTPException(status_code=409, detail="Gitea is not set up — run "
                            "`python -m backend.cli gitea-setup` on the host")


async def _call(coro):
    try:
        return await coro
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except gitea.GiteaOff as e:
        raise HTTPException(status_code=409, detail=str(e))
    except gitea.GiteaNotFound as e:
        raise HTTPException(status_code=404, detail=gitea.scrub(str(e)))
    except gitea.GiteaError as e:
        raise HTTPException(status_code=502, detail=gitea.scrub(str(e)))


@router.get("/status")
async def status():
    return await gitea.status()


@router.get("/repos")
async def repos():
    _need()
    return {"repos": await _call(gitea.list_repos())}


def _project(slug: str) -> None:
    if not gitea._SLUG.match(slug) or not (settings.projects_dir / slug / "project.md").exists():
        raise HTTPException(status_code=404, detail="no such project")


@router.get("/repos/{slug}/branches")
async def branches(slug: str):
    _need()
    return {"branches": await _call(gitea.branches(slug))}


@router.get("/repos/{slug}/commits")
async def commits(slug: str, branch: str = "main", page: int = 1, limit: int = 10):
    _need()
    return await _call(gitea.commits(slug, branch, page, limit))


@router.get("/repos/{slug}/pulls")
async def pulls(slug: str):
    """The agent's pull requests (Jav3's own requests of kind push), waiting
    and recently decided. Reconciles with Gitea, so read it on open or on a
    click, not on a timer."""
    _need()
    return await _call(gitea.agent_pulls(slug))


@router.get("/repos/{slug}/pulls/{number}/diff")
async def pull_diff(slug: str, number: int):
    _need()
    return await _call(gitea.pull_diff(slug, number))


@router.get("/repos/{slug}/sync")
async def sync(slug: str):
    """The host's main against Gitea's main."""
    _need()
    _project(slug)
    return await _call(gitea.sync_status(slug))


@router.post("/repos/{slug}/push")
async def push_main(slug: str):
    """Push main: the existing push_main, as the operator; see push_host_main."""
    _need()
    _project(slug)
    return await _call(gitea.push_host_main(slug))


@router.get("/repos/{slug}/access")
async def access(slug: str):
    _need()
    return await _call(gitea.repo_access(slug))


class Access(BaseModel):
    permission: str


@router.put("/repos/{slug}/access/{login}")
async def set_access(slug: str, login: str, body: Access):
    _need()
    return await _call(gitea.set_access(slug, login, body.permission))


@router.post("/repos/{slug}")
async def link_repo(slug: str):
    """Create/link the project's repo now (it is also done lazily)."""
    _need()
    d = settings.projects_dir / slug
    if not gitea._SLUG.match(slug) or not (d / "project.md").exists():
        raise HTTPException(status_code=404, detail="no such project")
    await _call(gitea.ensure_repo(slug))
    return {"url": gitea.web_url(slug)}


@router.get("/users")
async def users():
    _need()
    return {"users": await _call(gitea.list_users())}


class NewUser(BaseModel):
    login: str
    email: str = ""
    password: str


@router.post("/users")
async def create_user(body: NewUser):
    _need()
    return await _call(gitea.create_user(body.login.strip(), body.email.strip(),
                                         body.password))


class Password(BaseModel):
    password: str


@router.post("/users/{login}/password")
async def reset_password(login: str, body: Password):
    _need()
    await _call(gitea.reset_password(login, body.password))
    return {"ok": True}


class Disabled(BaseModel):
    disabled: bool = True


@router.post("/users/{login}/disable")
async def disable(login: str, body: Disabled):
    _need()
    shared = await _call(gitea.set_disabled(login, body.disabled))
    return {"ok": True, "disabled": body.disabled, **shared}
