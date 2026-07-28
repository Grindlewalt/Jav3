"""Operator API for the isolated triage reviewer (backend/reviewer.py).

Read status/audit-log, toggle the auto sweep, kick a run, undo an auto-action.
Runs execute detached (a 900-item backlog takes minutes) — the panel polls
GET / for `running` and the last-run summary.
"""
import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import reviewer
from .auth import require_user
from .db import get_db

router = APIRouter(prefix="/api/reviewer", tags=["reviewer"],
                   dependencies=[Depends(require_user)])


@router.get("")
async def status():
    db = await get_db()
    try:
        return await reviewer.status(db)
    finally:
        await db.close()


class ToggleBody(BaseModel):
    enabled: bool


@router.put("")
async def toggle(body: ToggleBody):
    db = await get_db()
    try:
        await reviewer.set_enabled(db, body.enabled)
        return await reviewer.status(db)
    finally:
        await db.close()


@router.post("/run")
async def run_now():
    if reviewer._lock.locked():
        return {"ok": False, "running": True}
    asyncio.create_task(reviewer.run(source="manual"))
    return {"ok": True, "running": True}


@router.get("/log")
async def log(limit: int = 100):
    db = await get_db()
    try:
        async with db.execute(
                "SELECT id, run_id, item_kind, item_id, project_slug, subject, verdict, "
                "reason, action, undone, created_at FROM triage_log "
                "ORDER BY id DESC LIMIT ?", (min(limit, 500),)) as cur:
            return {"log": [dict(r) for r in await cur.fetchall()]}
    finally:
        await db.close()


@router.post("/log/{lid}/undo")
async def undo(lid: int):
    db = await get_db()
    try:
        return await reviewer.undo(db, lid)
    finally:
        await db.close()
