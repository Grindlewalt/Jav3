"""Sandbox VM control surface.

Thin HTTP layer over backend.agent.tools.vm — status, lifecycle, run a
command, and move files host<->VM. The VM side of a project always lives at
/workspace/<slug>; pulls land inside the project dir (path-guarded), never
anywhere else on the host.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import gate
from .agent.tools import vm
from .auth import require_user
from .config import settings
from .db import get_db
from .fsutil import safe_join
from .staging import effective_read

router = APIRouter(prefix="/api/vm", tags=["vm"], dependencies=[Depends(require_user)])


class RunBody(BaseModel):
    command: str
    timeout: float | None = None
    cwd: str | None = None


class PushBody(BaseModel):
    project: str
    subpath: str = ""          # push only this part of the project dir


class PullBody(BaseModel):
    project: str
    remote_path: str           # relative to /workspace
    dest: str = "vm-results"   # subdir of the project dir to extract into


class NukeBody(BaseModel):
    confirm: bool = False


def _project_dir(slug: str):
    d = settings.projects_dir / slug
    if not d.is_dir() or not (d / "project.md").exists():
        raise HTTPException(status_code=404, detail="no such project")
    return d


@router.get("/status")
async def status():
    return await vm.status()


@router.post("/start")
async def start():
    try:
        await vm.start()
    except vm.VMError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return await vm.status()


@router.post("/stop")
async def stop():
    try:
        await vm.stop()
    except vm.VMError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return await vm.status()


@router.post("/nuke")
async def nuke(body: NukeBody):
    """Throw away the VM's entire disk and boot fresh from the golden image.
    Recovery action — requires explicit confirmation."""
    if not body.confirm:
        raise HTTPException(status_code=400, detail="nuke requires confirm=true")
    try:
        await vm.nuke()
    except vm.VMError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return await vm.status()


@router.post("/run")
async def run(body: RunBody):
    if not body.command.strip():
        raise HTTPException(status_code=400, detail="empty command")
    try:
        return await vm.run(body.command, timeout=body.timeout, cwd=body.cwd)
    except vm.VMError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/push")
async def push(body: PushBody):
    project_dir = _project_dir(body.project)
    src = safe_join(project_dir, body.subpath) if body.subpath else project_dir
    if not src.is_dir():
        raise HTTPException(status_code=400, detail="subpath is not a directory")
    try:
        return await vm.push(src, body.project)
    except vm.VMError as e:
        raise HTTPException(status_code=502, detail=str(e))


class GateRunBody(BaseModel):
    project: str
    command: str
    timeout: float | None = None
    fresh: bool = True


@router.post("/gate/run")
async def gate_run(body: GateRunBody):
    """M4 monitored execution: fresh VM, full capture, staged gate report."""
    _project_dir(body.project)
    if not body.command.strip():
        raise HTTPException(status_code=400, detail="empty command")
    try:
        return await gate.run_gated(body.project, body.command,
                                    timeout=body.timeout, fresh=body.fresh)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except vm.VMError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/gate/runs")
async def gate_runs(project: str | None = None):
    db = await get_db()
    try:
        q = ("SELECT r.id, p.slug, r.status, r.exec_log_path, r.net_log_path, "
             "r.pushed, r.created_at FROM runs r JOIN projects p ON p.id = r.project_id")
        args: tuple = ()
        if project:
            q += " WHERE p.slug = ?"
            args = (project,)
        q += " ORDER BY r.id DESC LIMIT 50"
        cur = await db.execute(q, args)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.get("/gate/runs/{run_id}/report")
async def gate_report(run_id: int):
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT p.slug FROM runs r JOIN projects p ON p.id = r.project_id "
            "WHERE r.id = ?", (run_id,))
        row = await cur.fetchone()
    finally:
        await db.close()
    if not row:
        raise HTTPException(status_code=404, detail="no such run")
    slug = row["slug"]
    p = effective_read(slug, f"runs/gate-{run_id}/report.md")
    if p is None:
        raise HTTPException(status_code=404, detail="no report for this run")
    return {"run_id": run_id, "project": slug, "report": p.read_text(),
            "staged": ".staging" in str(p)}


@router.post("/pull")
async def pull(body: PullBody):
    project_dir = _project_dir(body.project)
    if ".." in body.remote_path or body.remote_path.startswith("/"):
        raise HTTPException(status_code=400, detail="remote_path must be relative")
    dest = safe_join(project_dir, body.dest)
    try:
        return await vm.pull(body.remote_path, dest)
    except vm.VMError as e:
        raise HTTPException(status_code=502, detail=str(e))
