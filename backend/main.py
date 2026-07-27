from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import asyncio

from . import (agents_api, agents_run, artifacts_api, auth, chat, egress_api,
               git_api, gui, guest_shell, logs_api, memory_api,
               notifications_api, projects, runs_api, schedules, skills_api,
               vm_api, workspace, secrets)
from .agent.model import (MODEL_STATE_KEY, get_model_override,
                          load_model_override, set_model_override)
from .agent.tools.registry import compile_registry
from .auth import require_user
from .config import settings, ensure_dirs
from .db import get_db, init_db, set_state
from .memory import ensure_memory_seeds
from .vm.egress_proxy import proxy as egress_proxy
from .vm.gateway_server import gateway
from .vm.lifecycle import reaper_loop, vm


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    await init_db()
    ensure_memory_seeds()
    await load_model_override()    # nav model switch survives restarts
    await schedules.ensure_default_schedules()
    compile_registry()
    task = asyncio.create_task(schedules.scheduler_loop())
    reaper = asyncio.create_task(reaper_loop())   # idle guest scrub (M4c)
    await gateway.start()          # host vsock model gateway (no-op if no vsock)
    if settings.vm_egress:
        await vm.net_up()          # tap/nft/dnsmasq/pcap up BEFORE the proxy binds
    await egress_proxy.start()     # monitored-egress proxy (no-op unless vm_egress)
    await guest_shell.start_unix_server()   # co-working shell CLI front door
    try:
        yield
    finally:
        task.cancel()
        reaper.cancel()
        await gateway.stop()
        await egress_proxy.stop()
        await guest_shell.stop_unix_server()
        await vm.teardown()        # never leave a guest running past shutdown
        if settings.vm_egress:
            await vm.net_down()


app = FastAPI(title="Jarvis v3", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(chat.router)
app.include_router(memory_api.router)
app.include_router(workspace.router)
app.include_router(skills_api.router)
app.include_router(agents_api.router)
app.include_router(agents_run.router)
app.include_router(schedules.router)
app.include_router(runs_api.router)
app.include_router(runs_api.jobs_router)
app.include_router(git_api.router)
app.include_router(notifications_api.router)
app.include_router(logs_api.router)
app.include_router(secrets.router)
app.include_router(artifacts_api.router)
app.include_router(vm_api.router)
app.include_router(egress_api.router)
app.include_router(egress_api.security_router)
app.include_router(gui.router)
app.include_router(guest_shell.router)


@app.get("/api/health")
async def health():
    return {"ok": True}


class ModelSelect(BaseModel):
    model: str


def _model_state() -> dict:
    return {"active": get_model_override() or settings.model_name,
            "default": settings.model_name, "choices": settings.model_choices}


@app.get("/api/model", dependencies=[Depends(require_user)])
async def get_model():
    return _model_state()


@app.put("/api/model", dependencies=[Depends(require_user)])
async def put_model(body: ModelSelect):
    """Switch the runtime model (nav dropdown). Takes effect on the next model
    call — no restart. Agents with an explicit model pin are unaffected."""
    if body.model not in settings.model_choices:
        raise HTTPException(status_code=400,
                            detail=f"model must be one of {settings.model_choices}")
    override = None if body.model == settings.model_name else body.model
    set_model_override(override)
    db = await get_db()
    try:
        await set_state(db, MODEL_STATE_KEY, override or "")
    finally:
        await db.close()
    return _model_state()


@app.get("/api/config")
async def client_config():
    # Non-sensitive client config the SPA needs at boot. media_hosts is the
    # allowlist the render surfaces use to decide which remote media may load.
    return {"media_hosts": settings.media_hosts}


# Built SPA. In dev (no dist yet) the API still runs; the GUI just isn't served.
if (settings.frontend_dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=settings.frontend_dist / "assets"),
              name="assets")

    # The HTML shell carries no content hash, so it must never be heuristically
    # cached: without this, browsers guess a freshness window from Last-Modified
    # and keep serving a stale index.html (pointing at an old, also-cached JS
    # hash) across reloads — so a deploy silently never reaches the browser.
    # no-cache = may store, but must revalidate first (cheap 304 when unchanged).
    _shell_headers = {"Cache-Control": "no-cache"}

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = settings.frontend_dist / full_path
        if full_path and candidate.is_file():
            headers = _shell_headers if candidate.suffix == ".html" else None
            return FileResponse(candidate, headers=headers)
        return FileResponse(settings.frontend_dist / "index.html",
                            headers=_shell_headers)
