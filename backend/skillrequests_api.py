"""Operator API for skill install requests (docs/agent-requests-contract.md, 2).

Cookie-only (require_user). Prefixed /api/skill-requests, not /api/skills/,
whose /{slug} route would swallow it. An agent's only way in is the
`skill_request` tool, which can only file a pending row."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import skillimport, skillrequests
from .auth import require_user
from .db import get_db

router = APIRouter(prefix="/api/skill-requests", tags=["skill-requests"],
                   dependencies=[Depends(require_user)])


class ApproveBody(BaseModel):
    acknowledge: bool = False
    skill_sha256: str = ""


class RejectBody(BaseModel):
    reason: str = ""


@router.get("")
async def list_requests(status: str | None = None, project: str | None = None):
    db = await get_db()
    try:
        return {"requests": await skillrequests.list_rows(db, status, project)}
    finally:
        await db.close()


@router.post("/{req_id}/fetch")
async def fetch_request(req_id: int):
    db = await get_db()
    try:
        try:
            return await skillrequests.fetch(db, req_id)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except skillrequests.SkillRequestError as e:
            raise HTTPException(409, str(e))
        except skillimport.SkillImportError as e:
            raise HTTPException(400, str(e))
    finally:
        await db.close()


@router.post("/{req_id}/approve")
async def approve_request(req_id: int, body: ApproveBody,
                          user: dict = Depends(require_user)):
    if not body.acknowledge:
        raise HTTPException(400, "approval needs acknowledge: true")
    db = await get_db()
    try:
        try:
            return await skillrequests.approve(
                db, req_id, skill_sha256=body.skill_sha256,
                decided_by=user.get("username") or "operator", by_operator=True)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except skillrequests.SkillRequestError as e:
            raise HTTPException(409, str(e))
    finally:
        await db.close()


@router.post("/{req_id}/reject")
async def reject_request(req_id: int, body: RejectBody,
                         user: dict = Depends(require_user)):
    db = await get_db()
    try:
        try:
            return await skillrequests.reject(
                db, req_id, reason=body.reason,
                decided_by=user.get("username") or "operator", by_operator=True)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except skillrequests.SkillRequestError as e:
            raise HTTPException(409, str(e))
    finally:
        await db.close()
