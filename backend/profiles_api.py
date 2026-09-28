"""Security profiles API (DESIGN-BOXES (d); docs/boxes-contract.md J (c)/(d)).

Cookie-only (require_user reads the session cookie and nothing else) and
same-origin gated like every control-plane state change. Every write is a
`profile_changed` security event carrying a field diff.

  GET    /api/profiles                  {profiles:[row + projects:[slugs]]}
  POST   /api/profiles                  create; service_placement/box_runtime default per_project/kvm
  PUT    /api/profiles/{id}             edit;   service_placement + box_runtime REQUIRED (422)
  DELETE /api/profiles/{id}             the default 409; in-use 409
  POST   /api/profiles/{id}/default     mark it the default for new/unassigned projects
  PUT    /api/projects/{slug}/profile   {profile_id}
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import profiles
from .auth import require_user
from .db import get_db

router = APIRouter(prefix="/api/profiles", tags=["profiles"],
                   dependencies=[Depends(require_user)])
project_router = APIRouter(prefix="/api/projects", tags=["profiles"],
                           dependencies=[Depends(require_user)])


class ProfileBody(BaseModel):
    # no defaults on purpose: a field left out of an edit keeps its value, and
    # the two required ones are refused when missing (profiles.validate)
    name: str | None = None
    default_verdict: str | None = None
    network_off: bool | None = None
    allow_hosts: list[str] | None = None
    deny_hosts: list[str] | None = None
    secrets: list[str] | None = None
    auto_handle: bool | None = None
    separate_box: bool | None = None
    box_image: str | None = None
    box_mem_mb: int | None = None
    box_runtime: str | None = None
    allow_services: bool | None = None
    allow_package_requests: bool | None = None
    service_placement: str | None = None


def _body(b: ProfileBody) -> dict:
    return b.model_dump(exclude_unset=True)


def _actor(user: dict) -> str:
    return str(user.get("username") or "operator")


def _fail(e: profiles.ProfileError):
    raise HTTPException(status_code=e.status, detail=str(e))


@router.get("")
async def list_profiles():
    db = await get_db()
    try:
        return {"profiles": [profiles.api_row(p) for p in await profiles.list_all(db)]}
    finally:
        await db.close()


async def _row_with_projects(db, pid: int) -> dict:
    for p in await profiles.list_all(db):
        if p["id"] == pid:
            return profiles.api_row(p)
    raise HTTPException(status_code=404, detail="no such profile")


@router.post("")
async def create_profile(body: ProfileBody, user: dict = Depends(require_user)):
    db = await get_db()
    try:
        try:
            p = await profiles.create(db, _body(body), actor=_actor(user))
        except profiles.ProfileError as e:
            _fail(e)
        return await _row_with_projects(db, p["id"])
    finally:
        await db.close()


@router.put("/{pid}")
async def edit_profile(pid: int, body: ProfileBody, user: dict = Depends(require_user)):
    db = await get_db()
    try:
        try:
            await profiles.update(db, pid, _body(body), actor=_actor(user))
        except profiles.ProfileError as e:
            _fail(e)
        return await _row_with_projects(db, pid)
    finally:
        await db.close()


@router.delete("/{pid}")
async def delete_profile(pid: int, user: dict = Depends(require_user)):
    db = await get_db()
    try:
        try:
            return await profiles.delete(db, pid, actor=_actor(user))
        except profiles.ProfileError as e:
            _fail(e)
    finally:
        await db.close()


@router.post("/{pid}/default")
async def make_default(pid: int, user: dict = Depends(require_user)):
    db = await get_db()
    try:
        try:
            await profiles.set_default(db, pid, actor=_actor(user))
        except profiles.ProfileError as e:
            _fail(e)
        return await _row_with_projects(db, pid)
    finally:
        await db.close()


class AssignBody(BaseModel):
    profile_id: int


@project_router.put("/{slug}/profile")
async def assign_profile(slug: str, body: AssignBody, user: dict = Depends(require_user)):
    db = await get_db()
    try:
        try:
            return await profiles.assign(db, slug, body.profile_id, actor=_actor(user))
        except profiles.ProfileError as e:
            _fail(e)
    finally:
        await db.close()
