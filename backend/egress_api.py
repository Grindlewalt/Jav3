"""Egress control surface: approve the agent's connection requests, apply the
per-project dev preset, and drive the global YOLO switch. Approvals land in the
same deny-by-default nft allowlist the sandbox console manages."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import egress
from .auth import require_user
from .config import settings

router = APIRouter(prefix="/api/egress", tags=["egress"],
                   dependencies=[Depends(require_user)])


@router.get("/requests")
async def list_requests(status: str = "pending", project: str | None = None):
    return {"requests": await egress.list_requests(status=status, slug=project)}


class ApproveBody(BaseModel):
    ttl_minutes: int | None = None


@router.post("/requests/{rid}/approve")
async def approve(rid: int, body: ApproveBody):
    try:
        return await egress.approve_request(rid, ttl_minutes=body.ttl_minutes)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/requests/{rid}/deny")
async def deny(rid: int):
    try:
        return await egress.deny_request(rid)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


class DevPresetBody(BaseModel):
    ttl_minutes: int | None = 480      # a working day by default


@router.post("/preset/{slug}")
async def dev_preset(slug: str, body: DevPresetBody):
    if not (settings.projects_dir / slug / "project.md").exists():
        raise HTTPException(status_code=404, detail="no such project")
    await egress.set_project_mode(slug, "dev")
    return await egress.apply_dev_preset(slug, ttl_minutes=body.ttl_minutes)


class YoloBody(BaseModel):
    ttl_minutes: int = 60


@router.get("/yolo")
async def yolo_status():
    return await egress.yolo_status()


@router.post("/yolo")
async def yolo_on(body: YoloBody):
    """Open the sandbox VM's egress entirely — defeats the exfiltration guard.
    TTL'd and auto-closing; use only on a VM holding nothing sensitive."""
    return await egress.yolo_on(ttl_minutes=body.ttl_minutes)


@router.delete("/yolo")
async def yolo_off():
    return await egress.yolo_off()
