"""Operator control surface for the sandbox VM (Phase 2): status / boot /
teardown / nuke / selftest. The guest is disposable — nuke discards its overlay
disk and reboots fresh from the golden image. `selftest` boots the guest, lets
its stub reach the host model gateway over vsock for one completion, and returns
the reply — the end-to-end proof that the host<->guest model path works."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .vm.lifecycle import VMError, vm

router = APIRouter(prefix="/api/vm", tags=["vm"], dependencies=[Depends(require_user)])


class NukeBody(BaseModel):
    confirm: bool = False


@router.get("/status")
async def status():
    return vm.status()


@router.post("/boot")
async def boot():
    try:
        await vm.boot()
    except VMError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return vm.status()


@router.post("/teardown")
async def teardown():
    await vm.teardown()
    return vm.status()


@router.post("/nuke")
async def nuke(body: NukeBody):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="nuke requires confirm=true")
    try:
        await vm.nuke()
    except VMError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return vm.status()


@router.post("/selftest")
async def selftest():
    try:
        return await vm.selftest()
    except VMError as e:
        raise HTTPException(status_code=502, detail=str(e))
