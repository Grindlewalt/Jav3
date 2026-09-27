"""Settings > Gitea: status, the repo list, and the account basics (list,
create, reset a password, disable). Operator-only: require_user takes the
password session cookie and nothing else, so a device token (chat or cli)
never reaches these. Every call goes to Gitea's admin API host-side with the
operator's token, which never leaves the host."""
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
    except gitea.GiteaError as e:
        raise HTTPException(status_code=502, detail=gitea.scrub(str(e)))


@router.get("/status")
async def status():
    return await gitea.status()


@router.get("/repos")
async def repos():
    _need()
    return {"repos": await _call(gitea.list_repos())}


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
    await _call(gitea.set_disabled(login, body.disabled))
    return {"ok": True, "disabled": body.disabled}
