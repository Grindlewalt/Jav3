"""Operator API for environment requests (docs/agent-requests-contract.md, 1).

Cookie-only (require_user) and same-origin gated like packages_api. An agent's
only way in is the `environment_request` tool, which can only file a pending
row.

    GET  /api/environments?status=&project=
    POST /api/environments/{id}/approve     {acknowledge:true, build:true}
    POST /api/environments/{id}/reject      {reason}
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import environments, packages
from .auth import require_user
from .config import settings
from .db import get_db
from .packages_api import _start_build
from .vm import images

router = APIRouter(prefix="/api/environments", tags=["environments"],
                   dependencies=[Depends(require_user)])


class ApproveBody(BaseModel):
    acknowledge: bool = False
    build: bool = True


class RejectBody(BaseModel):
    reason: str = ""


@router.get("")
async def list_environments(status: str | None = None, project: str | None = None):
    db = await get_db()
    try:
        return {"environments": await environments.list_rows(db, status, project)}
    finally:
        await db.close()


@router.post("/{env_id}/approve")
async def approve(env_id: int, body: ApproveBody, user: dict = Depends(require_user)):
    if not body.acknowledge:
        raise HTTPException(400, "approval needs acknowledge: true")
    db = await get_db()
    try:
        try:
            row = await environments.approve(
                db, env_id, decided_by=user.get("username") or "operator",
                by_operator=True)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except (packages.PackageError, images.RecipeError) as e:
            raise HTTPException(409, str(e))
    finally:
        await db.close()
    # true only when THIS call started a build (see packages_api.approve)
    started = bool(body.build and settings.vm_boxes_enabled
                   and not images.builder.lock.locked())
    if started:
        _start_build(row["name"])
    return {**row, "build_started": started}


@router.post("/{env_id}/reject")
async def reject(env_id: int, body: RejectBody, user: dict = Depends(require_user)):
    db = await get_db()
    try:
        try:
            return await environments.reject(
                db, env_id, reason=body.reason,
                decided_by=user.get("username") or "operator", by_operator=True)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except packages.PackageError as e:
            raise HTTPException(409, str(e))
    finally:
        await db.close()
