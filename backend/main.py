from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import asyncio

from . import (agents_api, agents_run, artifacts_api, auth, chat, egress_api, git_api,
               logs_api, memory_api, notifications_api, projects, runs_api, sandbox,
               sandbox_api, schedules, skills_api, vm_api, workspace, secrets)
from .agent.tools.registry import compile_registry
from .config import settings, ensure_dirs
from .db import init_db
from .memory import ensure_memory_seeds


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    await init_db()
    ensure_memory_seeds()
    await schedules.ensure_default_schedules()
    compile_registry()
    # re-program the learned egress allowlist into nftables (empty on boot);
    # best-effort so a dev host without the table starts fine
    try:
        await sandbox.sync_nft()
    except Exception:
        pass
    try:
        from . import egress
        await egress.ensure_dns_readable()   # unbreak the gate's DNS correlation
    except Exception:
        pass
    task = asyncio.create_task(schedules.scheduler_loop())
    try:
        yield
    finally:
        task.cancel()


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
app.include_router(vm_api.router)
app.include_router(git_api.router)
app.include_router(sandbox_api.router)
app.include_router(egress_api.router)
app.include_router(notifications_api.router)
app.include_router(logs_api.router)
app.include_router(secrets.router)
app.include_router(artifacts_api.router)


@app.get("/api/health")
async def health():
    return {"ok": True}


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
