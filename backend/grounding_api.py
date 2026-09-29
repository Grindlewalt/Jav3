"""Settings → Grounding: the model finder's routes (docs/navigation-contract.md
C). Cookie-only operator routes, like desk_api.router.

    GET  /api/grounding                -> grounding.status()
    POST /api/grounding/probe          {models?: [...]} -> {job}
    POST /api/grounding/probe/cancel   -> {cancelled}
    PUT  /api/grounding                {model: "provider/id" | ""} -> status
    GET  /api/grounding/fixtures/{i}.png   (what the models were tested on)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from . import grounding
from .auth import require_user

router = APIRouter(prefix="/api/grounding", tags=["grounding"],
                   dependencies=[Depends(require_user)])


class ProbeBody(BaseModel):
    models: list[str] | None = None


class PinBody(BaseModel):
    model: str = ""


@router.get("")
async def get_status() -> dict:
    return grounding.status()


@router.put("")
async def put_pin(body: PinBody) -> dict:
    try:
        return grounding.set_pinned(body.model)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.post("/probe")
async def post_probe(body: ProbeBody | None = None,
                     user: dict = Depends(require_user)) -> dict:
    try:
        job = await grounding.start_probe((body.models if body else None) or None,
                                          by=str(user.get("username") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except grounding.NotConfigured as e:
        raise HTTPException(409, str(e)) from None
    return {"job": job}


@router.post("/probe/cancel")
async def post_cancel() -> dict:
    return {"cancelled": grounding.cancel_probe()}


@router.get("/fixtures/{i}.png")
async def get_fixture(i: int) -> Response:
    from . import grounding_fixtures as gf
    if not gf.HAVE_PIL:
        raise HTTPException(503, "Pillow not installed")
    if i < 0 or i >= gf.count():
        raise HTTPException(404, "no such fixture")
    return Response(gf.fixture(i)["png"], media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})
