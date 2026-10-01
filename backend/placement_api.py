"""Per-project "Runs in" (backend/vm/placement.py). Operator only: cookie
session (require_user), same-origin gated like every control-plane write.

  GET /api/projects/{slug}/placement   setting, effective, boxes, images, budget
  PUT /api/projects/{slug}/placement   {mode: profile|shared|own|join[:<box>],
                                        runtime?, image?, mem_mb?, box_id?}
Applies from the project's next turn.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .db import get_db
from .vm import placement

router = APIRouter(prefix="/api/projects", tags=["placement"],
                   dependencies=[Depends(require_user)])


class PlacementBody(BaseModel):
    mode: str = "profile"
    runtime: str | None = None
    image: str | None = None
    mem_mb: int | None = None
    box_id: str | None = None


@router.get("/{slug}/placement")
async def get_placement(slug: str):
    db = await get_db()
    try:
        return await placement.overview(db, slug)
    except placement.PlacementError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    finally:
        await db.close()


@router.put("/{slug}/placement")
async def put_placement(slug: str, body: PlacementBody,
                        user: dict = Depends(require_user)):
    db = await get_db()
    try:
        out = await placement.put(db, slug, body.model_dump(),
                                  actor=str(user.get("username") or "operator"),
                                  by_operator=True)
        return {**await placement.overview(db, slug), "warnings": out["warnings"]}
    except placement.PlacementError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    finally:
        await db.close()
