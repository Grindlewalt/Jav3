"""The plan's HTTP face: the checklist file (`projects/<slug>/.plan.json`)
mirrored by a path-guarded API, plus start/stop for the runner. The Workspace
`plan` panel is the GUI; the `orchestrate` tool is the model's way in. Both
edit the same file under plan.py's lock, so the operator can change items
while a run is live and the runner honours it on its next tick.

Lives under /api/projects/{slug}/plan, next to the workspace router, so the
same-origin protection for cookie-authenticated mutations applies to it like
every other project route.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import plan as plan_mod
from .agent.model import in_peak_window
from .auth import require_user
from .workspace import project_dir
from .writes import SecretLeakError

router = APIRouter(prefix="/api/projects/{slug}/plan", tags=["plan"],
                   dependencies=[Depends(require_user)])


class PlanRequest(BaseModel):
    dump: str
    files: list[str] = []
    title: str = ""
    confirm_peak: bool = False


class PlanDoc(BaseModel):
    title: str | None = None
    items: list[dict] = []
    attempts_max: int | None = None
    max_concurrent: int | None = None
    max_iterations: int | None = None


class ItemIn(BaseModel):
    title: str | None = None
    brief: str | None = None
    depends_on: list[str] | None = None
    assignee: str | None = None
    status: str | None = None
    position: int | None = None


class RunRequest(BaseModel):
    confirm_peak: bool = False


def _out(slug: str, plan: dict | None) -> dict:
    return {"plan": plan_mod.public(plan, slug), "running": plan_mod.is_running(slug)}


@router.get("")
async def get_plan(slug: str):
    await project_dir(slug)
    return _out(slug, plan_mod.load(slug))


@router.post("")
async def make_plan(slug: str, body: PlanRequest):
    """The planner pass: dump (+ optional project file refs) -> checklist."""
    await project_dir(slug)
    if not body.dump.strip():
        raise HTTPException(status_code=400, detail="dump is required")
    if plan_mod.is_running(slug):
        raise HTTPException(status_code=409, detail="plan_running")
    if in_peak_window() and not body.confirm_peak:
        raise HTTPException(status_code=409, detail="peak_confirmation_required")
    try:
        plan = await plan_mod.plan_from_dump(slug, body.dump, body.files, title=body.title)
    except SecretLeakError as e:
        raise HTTPException(status_code=400, detail=f"refused: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _out(slug, plan)


@router.put("")
async def put_plan(slug: str, body: PlanDoc):
    """Replace the checklist wholesale (the panel's reorder/bulk edit). Creates
    the plan if there is none. Runner-owned fields survive by item id."""
    await project_dir(slug)
    try:
        async with plan_mod._lock(slug):
            plan = plan_mod.load(slug) or plan_mod.empty_plan()
            if body.title is not None:
                plan["title"] = body.title.strip()[:120]
            for k in ("attempts_max", "max_concurrent", "max_iterations"):
                if getattr(body, k) is not None:
                    plan[k] = getattr(body, k)
            plan_mod.replace_items(plan, body.items)
            plan = plan_mod.normalise(plan)
            await plan_mod.save(slug, plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SecretLeakError as e:
        raise HTTPException(status_code=400, detail=f"refused: {e}")
    return _out(slug, plan)


@router.post("/items")
async def add_item(slug: str, body: ItemIn):
    await project_dir(slug)
    if not (body.title or "").strip():
        raise HTTPException(status_code=400, detail="title is required")
    try:
        async with plan_mod._lock(slug):
            plan = plan_mod.load(slug) or plan_mod.empty_plan()
            it = plan_mod.new_item(plan, title=body.title)
            plan["items"].append(it)
            plan_mod.apply_item_edit(plan, it, body.model_dump(exclude_none=True))
            plan = plan_mod.normalise(plan)
            await plan_mod.save(slug, plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _out(slug, plan)


@router.patch("/items/{item_id}")
async def patch_item(slug: str, item_id: str, body: ItemIn):
    await project_dir(slug)
    try:
        async with plan_mod.edit(slug) as plan:
            it = plan_mod.index(plan).get(item_id)
            if it is None:
                raise HTTPException(status_code=404, detail="no such item")
            plan_mod.apply_item_edit(plan, it, body.model_dump(exclude_none=True))
            plan_mod.normalise(plan)
    except LookupError:
        raise HTTPException(status_code=404, detail="no plan")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _out(slug, plan)


@router.delete("/items/{item_id}")
async def delete_item(slug: str, item_id: str):
    await project_dir(slug)
    try:
        async with plan_mod.edit(slug) as plan:
            before = len(plan["items"])
            plan["items"] = [it for it in plan["items"] if it["id"] != item_id]
            if len(plan["items"]) == before:
                raise HTTPException(status_code=404, detail="no such item")
            plan_mod.normalise(plan)          # dependants of the deleted item drop the edge
    except LookupError:
        raise HTTPException(status_code=404, detail="no plan")
    return _out(slug, plan)


@router.post("/run")
async def run_plan(slug: str, body: RunRequest):
    """Start the runner, detached: the response returns at once with the head
    conversation to follow on /api/runs/{root_id}/stream."""
    await project_dir(slug)
    if in_peak_window() and not body.confirm_peak:
        raise HTTPException(status_code=409, detail="peak_confirmation_required")
    try:
        started = await plan_mod.start_run(slug, peak=body.confirm_peak)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {**started, **_out(slug, plan_mod.load(slug))}


@router.post("/stop")
async def stop_plan(slug: str):
    await project_dir(slug)
    # the panel takes every response as the plan's state, so this one carries it
    return {"stopped": plan_mod.stop_run(slug), **_out(slug, plan_mod.load(slug))}
