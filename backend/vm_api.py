"""Operator control surface for the sandbox VM (Phase 2): status / boot /
teardown / nuke / selftest. The guest is disposable — nuke discards its overlay
disk and reboots fresh from the golden image. `selftest` boots the guest, lets
its stub reach the host model gateway over vsock for one completion, and returns
the reply — the end-to-end proof that the host<->guest model path works."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .vm import boxes
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


@router.post("/rebuild")
async def rebuild(body: NukeBody):
    """Build the next golden-image version (patched kernel) in the background.
    Double-confirmed like nuke — it's a ~20-minute Pi operation."""
    if not body.confirm:
        raise HTTPException(status_code=400, detail="rebuild requires confirm=true")
    return await vm.rebuild_image()


# --- boxes (DESIGN-BOXES.md (e); docs/boxes-contract.md J) -------------------
# The routes above stay as the shared box's aliases. These list and drive
# every box. With boxes off the list is just the shared box.

class DestroyBody(BaseModel):
    confirm: bool = False
    delete_data: bool = False


def _box_or_404(box_id: str, *, allocate: bool = False):
    b = boxes.get(box_id)
    if b is None and allocate and boxes.enabled() and box_id.startswith("p-"):
        try:
            b = boxes.allocate("project", project=box_id[2:])
        except boxes.BoxCapError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except boxes.BoxError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if b is None:
        raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
    return b


async def runtimes() -> dict:
    """{kvm:{available,reason}, docker:{available, reason, rootless, userns,
    gvisor, seccomp, weak, warnings}} (docs/docker-runtime.md 5). The docker
    driver is imported only when docker_enabled is on."""
    from .config import settings
    if settings.docker_enabled:
        from .vm import docker_runtime
        return await docker_runtime.runtimes_json()
    from .vm.gateway_server import gateway
    import os
    kvm = ({"available": False, "reason": "no /dev/kvm on this host"}
           if not os.path.exists("/dev/kvm") else
           {"available": False, "reason": "vsock gateway not running"}
           if not gateway.enabled else {"available": True, "reason": None})
    return {"kvm": kvm,
            "docker": {"available": False,
                       "reason": "docker runtime is off (docker_enabled)",
                       "rootless": None, "userns": None, "gvisor": None,
                       "seccomp": None, "weak": None, "warnings": []}}


async def _box_row(b) -> dict:
    row = boxes.status_json(b)
    row["image_pending"] = None
    if b.kind == "project":
        # the profile's box_image changed under an allocated box: a stopped
        # box picks it up at its next start, a running one after a restart
        prof = await boxes.project_profile(b.project) or {}
        want = prof.get("box_image") or "main"
        if prof.get("separate_box") and want != b.image[0]:
            row["image_pending"] = want
            row["restart_needed"] = True
    return row


@router.get("/boxes")
async def list_boxes():
    return {"enabled": boxes.enabled(),
            "boxes": [await _box_row(b) for b in boxes.all_boxes()],
            "budget": boxes.budget(),
            "runtimes": await runtimes()}


async def _warm_project_box(box_id: str):
    """Operator warm-up of p-<slug> before its first turn: allocated with the
    project's profile (image, memory, runtime), exactly as the turn would."""
    b = boxes.get(box_id)
    if b is not None and b.kind == "project":
        prof = await boxes.project_profile(b.project) or {}
        if prof.get("separate_box"):
            boxes.follow_profile_image(b, prof.get("box_image"))
    if b is not None or not boxes.enabled() or not box_id.startswith("p-"):
        return _box_or_404(box_id)
    prof = await boxes.project_profile(box_id[2:]) or {}
    try:
        return boxes.allocate("project", project=box_id[2:],
                              variant=prof.get("box_image") or "main",
                              mem_mb=prof.get("box_mem_mb"),
                              runtime=prof.get("box_runtime") or "kvm")
    except boxes.BoxCapError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except boxes.BoxError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/boxes/{box_id}/start")
async def start_box(box_id: str):
    b = await _warm_project_box(box_id)
    try:
        await boxes.start(b)
    except (VMError, boxes.BoxError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    return await _box_row(b)


@router.post("/boxes/{box_id}/stop")
async def stop_box(box_id: str):
    b = _box_or_404(box_id)
    await boxes.stop(b)
    return await _box_row(b)


@router.post("/boxes/{box_id}/destroy")
async def destroy_box(box_id: str, body: DestroyBody):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="destroy requires confirm=true")
    b = _box_or_404(box_id)
    await boxes.destroy(b, delete_data=body.delete_data)
    return {"ok": True}
