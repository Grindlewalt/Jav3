from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (agents_api, agents_run, auth, chat, memory_api, projects,
               skills_api, vm_api, workspace)
from .agent.tools.registry import compile_registry
from .config import settings, ensure_dirs
from .db import init_db
from .memory import ensure_memory_seeds


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    await init_db()
    ensure_memory_seeds()
    compile_registry()
    yield


app = FastAPI(title="Jarvis v3", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(chat.router)
app.include_router(memory_api.router)
app.include_router(workspace.router)
app.include_router(skills_api.router)
app.include_router(agents_api.router)
app.include_router(agents_run.router)
app.include_router(vm_api.router)


@app.get("/api/health")
async def health():
    return {"ok": True}


# Built SPA. In dev (no dist yet) the API still runs; the GUI just isn't served.
if (settings.frontend_dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=settings.frontend_dist / "assets"),
              name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = settings.frontend_dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(settings.frontend_dist / "index.html")
