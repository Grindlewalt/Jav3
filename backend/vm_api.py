"""Operator control surface for the sandbox VM (Phase 2): status / boot /
teardown / nuke / selftest. The guest is disposable — nuke discards its overlay
disk and reboots fresh from the golden image. `selftest` boots the guest, lets
its stub reach the host model gateway over vsock for one completion, and returns
the reply — the end-to-end proof that the host<->guest model path works."""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .config import settings
from .vm import boxes, boxlog, leftovers, placement
from .vm.lifecycle import VMError, vm

router = APIRouter(prefix="/api/vm", tags=["vm"], dependencies=[Depends(require_user)])

# each running turn's current tool, for the box rows' "doing now" (boxlog.py)
boxlog.start_tracking()


class NukeBody(BaseModel):
    confirm: bool = False


def _who(user: dict) -> str:
    """The history's actor for an operator action."""
    return f"operator {user.get('username') or '?'}"


@router.get("/status")
async def status():
    return vm.status()


@router.post("/boot")
async def boot(user: dict = Depends(require_user)):
    try:
        with boxlog.by(_who(user)):
            await vm.boot()
    except VMError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return vm.status()


@router.post("/teardown")
async def teardown(user: dict = Depends(require_user)):
    with boxlog.by(_who(user)):
        await vm.teardown()
    return vm.status()


@router.post("/nuke")
async def nuke(body: NukeBody, user: dict = Depends(require_user)):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="nuke requires confirm=true")
    try:
        with boxlog.by(_who(user)):
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


async def _box_row(b, turns: dict | None = None, titles: dict | None = None,
                   last: dict | None = None) -> dict:
    row = boxes.status_json(b)
    # what it is doing now: the turns bound to it (host registries only), each
    # with its conversation's title and the tool running, if its channel said
    if turns is None:
        turns = boxlog.turns_by_box()
    now = [dict(t) for t in turns.get(b.id, [])]
    if titles is None:
        titles = await _titles({t["conversation_id"] for t in now})
    for t in now:
        t["title"] = titles.get(t["conversation_id"])
    row["now"] = now
    row["last_event"] = (last if last is not None else await boxlog.last_events()).get(b.id)
    row["image_pending"] = None
    if b.kind == "project":
        # the project's placement (its own, else its profile's) changed the
        # image under an allocated box: a stopped box picks it up at its next
        # start, a running one after a restart
        eff = await placement.effective(b.project)
        want = eff.get("image") or "main"
        if eff["mode"] == "own" and want != b.image[0]:
            row["image_pending"] = want
            row["restart_needed"] = True
    return row


async def _titles(ids) -> dict:
    """conversation id -> its clean title (one query for every row)."""
    ids = sorted(i for i in ids if isinstance(i, int))
    if not ids:
        return {}
    from .agenttree import clean_title
    from .db import get_db
    try:
        db = await get_db()
        try:
            async with db.execute(
                    "SELECT id, title, summary FROM conversations WHERE id IN (%s)"
                    % ",".join("?" * len(ids)), ids) as cur:
                return {r["id"]: clean_title(r["title"] or r["summary"]) or None
                        for r in await cur.fetchall()}
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — a title is decoration
        return {}


def _secs(v) -> int | None:
    return int(v) if v else None


@router.get("/boxes")
async def list_boxes():
    turns = boxlog.turns_by_box()
    titles = await _titles({t["conversation_id"] for ts in turns.values() for t in ts})
    last = await boxlog.last_events()
    return {"enabled": boxes.enabled(),
            "boxes": [await _box_row(b, turns, titles, last) for b in boxes.all_boxes()],
            "budget": boxes.budget(),
            "runtimes": await runtimes(),
            # the reaper's windows, for "idle 4m (stops at 10m)" and the scrub
            "idle": {"project_stop_s": _secs(settings.vm_box_idle_stop_seconds),
                     "shared_scrub_s": _secs(settings.vm_idle_scrub_seconds),
                     "reaper_interval_s": _secs(settings.vm_reaper_interval_seconds)}}


async def _warm_project_box(box_id: str):
    """Operator warm-up of p-<slug> before its first turn: allocated with the
    project's profile (image, memory, runtime), exactly as the turn would."""
    b = boxes.get(box_id)
    if b is not None and b.kind == "project":
        eff = await placement.effective(b.project)
        if eff["mode"] == "own":
            boxes.follow_profile_image(b, eff.get("image"))
    if b is not None or not boxes.enabled() or not box_id.startswith("p-"):
        return _box_or_404(box_id)
    from .db import get_db
    db = await get_db()
    try:
        known = await placement._project_exists(db, box_id[2:])
    finally:
        await db.close()
    if not known:      # a typo would reserve RAM and boot a box for nothing
        raise HTTPException(status_code=404, detail=f"no project {box_id[2:]!r}")
    eff = await placement.effective(box_id[2:])
    if eff["mode"] != "own":
        # warmed up the way the profile would make it (before placements a
        # warm box of a shared-box project was allowed and used; it still is)
        prof = await boxes.project_profile(box_id[2:]) or {}
        eff = placement.resolve({"mode": "own"}, prof, box_id[2:])
    try:
        return boxes.allocate("project", project=box_id[2:],
                              variant=eff.get("image") or "main",
                              mem_mb=eff.get("mem_mb"),
                              runtime=eff.get("runtime") or "kvm")
    except boxes.BoxCapError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except boxes.BoxError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/boxes/{box_id}/start")
async def start_box(box_id: str, user: dict = Depends(require_user)):
    b = await _warm_project_box(box_id)
    try:
        with boxlog.by(_who(user), "operator start"):
            await boxes.start(b)
    except (VMError, boxes.BoxError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    return await _box_row(b)


def _cut(b, what: str) -> str:
    """The history's reason for an operator action, saying when it ended
    turns that were running in the box."""
    n = int(getattr(b.ctl, "inflight", 0) or 0) if b.ctl is not None else 0
    return f"{what}, cutting off {n} running turn{'s' * (n != 1)}" if n else what


@router.post("/boxes/{box_id}/stop")
async def stop_box(box_id: str, user: dict = Depends(require_user)):
    b = _box_or_404(box_id)
    with boxlog.by(_who(user), _cut(b, "operator stop")):
        await boxes.stop(b)
    return await _box_row(b)


@router.post("/boxes/{box_id}/restart")
async def restart_box(box_id: str, user: dict = Depends(require_user)):
    """Stop and boot again from a fresh overlay (or a fresh container). Turns
    running in it are cut off; the operator's client says so first."""
    b = _box_or_404(box_id)
    try:
        with boxlog.by(_who(user), _cut(b, "operator restart")):
            await boxes.restart(b)
    except (VMError, boxes.BoxError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    return await _box_row(b)


@router.post("/boxes/{box_id}/destroy")
async def destroy_box(box_id: str, body: DestroyBody, user: dict = Depends(require_user)):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="destroy requires confirm=true")
    b = _box_or_404(box_id)
    with boxlog.by(_who(user), _cut(b, "operator destroy")):
        await boxes.destroy(b, delete_data=body.delete_data)
    # the shared box cannot be removed: destroy only stops it
    return {"ok": True, "removed": not b.is_shared}


_BOX_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,90}$")


@router.get("/boxes/{box_id}/events")
async def box_events(box_id: str, limit: int = 50, before: int | None = None):
    """The box's history, newest first: started, stopped, restarted,
    idle_stopped, wiped, nuked, destroyed, crashed, error, each with its
    reason and actor. A destroyed box's history still reads."""
    if not _BOX_ID_RE.match(box_id):
        raise HTTPException(status_code=400, detail="bad box id")
    evs = await boxlog.events(box_id, limit, before)
    if not evs and not before and boxes.get(box_id) is None:
        # a typo must not look like a box with no history
        raise HTTPException(status_code=404, detail=f"no box {box_id!r}, and no history of one")
    return {"box_id": box_id, "events": evs}


# --- leftovers (backend/vm/leftovers.py) ---------------------------------------------

class CleanBody(BaseModel):
    confirm: bool = False
    ids: list[str] | None = None


@router.get("/leftovers")
async def list_leftovers(fresh: bool = False):
    """What boxes left behind that no box of this server owns now, with why.
    Only this server's things are listed (see leftovers.py); nothing changes."""
    return await leftovers.scan(cached=not fresh)


@router.post("/leftovers/clean")
async def clean_leftovers(body: CleanBody):
    """Remove the cleanable leftovers (all, or `ids`), each identified again
    by a fresh scan first. Interfaces are never removed here."""
    if not body.confirm:
        raise HTTPException(status_code=400, detail="clean requires confirm=true")
    res = await leftovers.clean(body.ids)
    return {**res, "left": await leftovers.scan()}


# --- image build logs --------------------------------------------------------------

@router.get("/images/{variant}/log")
async def image_log(variant: str, version: int | None = None):
    """One image version's build log: the running build's (live), else that
    version's stored log (or the newest finished build's). `base` is the golden
    image's rebuild (build_base.sh), kept in memory since the app started.
    The lines are untrusted builder output: text, never markup."""
    if variant == "base":
        rl = vm.rebuild_log
        if not rl:
            raise HTTPException(status_code=404,
                                detail="no base image rebuild since the app started")
        return {"variant": "base", "version": rl.get("version"), "source": "memory",
                "running": bool(rl.get("running")), "phase": None,
                "ok": rl.get("ok"), "error": rl.get("error"),
                "finished_at": rl.get("finished_at"), "lines": list(rl.get("lines") or [])}
    from .vm import images
    try:
        images.check_variant_name(variant)
    except images.RecipeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job = images.builder.current
    if job is not None and job.variant == variant and version in (None, job.version):
        return {"variant": variant, "version": job.version, "source": "live",
                "running": True, "phase": images.builder.phase, "ok": None,
                "error": None, "finished_at": None, "lines": list(job.log)}
    from .db import get_db
    db = await get_db()
    try:
        q = ("SELECT version, status, build_log, built_at FROM image_versions "
             "WHERE variant = ? AND status IN ('built', 'failed')")
        args: list = [variant]
        if version is not None:
            q += " AND version = ?"
            args.append(version)
        async with db.execute(q + " ORDER BY version DESC LIMIT 1", args) as cur:
            r = await cur.fetchone()
    finally:
        await db.close()
    if r is None:
        raise HTTPException(status_code=404, detail=f"no finished build of {variant}"
                                                    + (f" v{version}" if version else ""))
    lb = images._last_build(dict(r))
    return {"variant": variant, "version": lb["version"], "source": "stored",
            "running": False, "phase": None, "ok": lb["ok"], "error": lb["error"],
            "finished_at": lb["finished_at"], "lines": lb["log_tail"]}
