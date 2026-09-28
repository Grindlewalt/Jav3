"""Settings → Grounding: the model finder's routes (docs/navigation-contract.md
C). Cookie-only operator routes, like desk_api.router. WP3 fills these in;
the contract commit mounts the router so main.py never changes again.

    GET  /api/grounding                -> grounding.status()
    POST /api/grounding/probe          {models?: [...]} -> {job}
    PUT  /api/grounding                {model: "provider/id" | ""} -> status
    GET  /api/grounding/fixtures/{i}.png
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from . import grounding
from .auth import require_user

router = APIRouter(prefix="/api/grounding", tags=["grounding"],
                   dependencies=[Depends(require_user)])


@router.get("")
async def get_status() -> dict:
    return grounding.status()
