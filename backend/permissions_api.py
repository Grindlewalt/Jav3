"""Permission modes and always-allow rules over HTTP (backend/permissions.py).

GET/PUT /api/chat/{cid}/permission_mode   any actor that may chat (the TUI's
    Shift+Tab and the web chat toolbar); {mode, explicit}
GET /api/permissions/rules, DELETE /api/permissions/rules/{id}
    operator only (Settings, /security): the "always allow" rules
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import permissions
from .auth import require_actor, require_user
from .db import get_db

router = APIRouter(prefix="/api", tags=["permissions"])


class ModeBody(BaseModel):
    mode: Literal["yolo", "auto", "ask"]


async def _explicit(cid: int) -> str | None:
    db = await get_db()
    try:
        async with db.execute("SELECT permission_mode FROM conversations WHERE id = ?",
                              (cid,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    if row is None:
        raise HTTPException(status_code=404, detail="no such conversation")
    return row[0]


@router.get("/chat/{cid}/permission_mode", dependencies=[Depends(require_actor)])
async def get_mode(cid: int):
    explicit = await _explicit(cid)
    return {"mode": await permissions.get_mode(cid), "explicit": explicit is not None,
            "modes": list(permissions.MODES)}


@router.put("/chat/{cid}/permission_mode", dependencies=[Depends(require_actor)])
async def put_mode(cid: int, body: ModeBody):
    await _explicit(cid)                 # 404 on an unknown conversation
    db = await get_db()
    try:
        await permissions.set_mode(db, cid, body.mode)
        await db.commit()
    finally:
        await db.close()
    return {"mode": body.mode, "explicit": True}


@router.get("/permissions/rules", dependencies=[Depends(require_user)])
async def list_rules():
    return {"rules": await permissions.list_rules()}


@router.delete("/permissions/rules/{rule_id}", dependencies=[Depends(require_user)])
async def delete_rule(rule_id: int):
    if not await permissions.delete_rule(rule_id):
        raise HTTPException(status_code=404, detail="no such rule")
    return {"ok": True}
