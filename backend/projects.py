import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .config import settings
from .db import get_db, set_state
from .memory import (
    PROJECT_TEMPLATE,
    assemble_system_prompt,
    get_active_project,
    project_md_path,
    read_project_md,
    refresh_all_projects,
)

router = APIRouter(prefix="/api", tags=["projects"], dependencies=[Depends(require_user)])


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise HTTPException(status_code=400, detail="name produces empty slug")
    return slug


class CreateProject(BaseModel):
    name: str
    summary: str = "(describe what you're building here)"


class UpdateProjectMd(BaseModel):
    content: str


@router.get("/projects")
async def list_projects():
    db = await get_db()
    try:
        async with db.execute(
            "SELECT id, slug, name, github_remote, created_at FROM projects "
            "WHERE deleted_at IS NULL AND is_hidden = 0 ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        async with db.execute(
            "SELECT id, slug, name, deleted_at FROM projects "
            "WHERE deleted_at IS NOT NULL AND is_hidden = 0 ORDER BY deleted_at DESC"
        ) as cur:
            deleted = await cur.fetchall()
        active = await get_active_project(db)
    finally:
        await db.close()
    return {"projects": [dict(r) for r in rows],
            "deleted": [dict(r) for r in deleted], "active": active}


@router.post("/projects")
async def create_project(body: CreateProject):
    slug = slugify(body.name)
    project_dir = settings.projects_dir / slug
    db = await get_db()
    try:
        async with db.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)) as cur:
            if await cur.fetchone():
                raise HTTPException(status_code=409, detail=f"project '{slug}' already exists")
        for sub in ("code", "notes"):
            (project_dir / sub).mkdir(parents=True, exist_ok=True)
        md = project_md_path(slug)
        if not md.exists():
            md.write_text(PROJECT_TEMPLATE.format(
                name=body.name, summary=body.summary, created=date.today().isoformat()))
        await db.execute(
            "INSERT INTO projects (slug, name, path) VALUES (?, ?, ?)",
            (slug, body.name, str(project_dir)),
        )
        await db.commit()
        await refresh_all_projects(db)
    finally:
        await db.close()
    # with direct writes (no staging quarantine) git is the review/undo surface,
    # so every project is a repo from birth with a baseline commit to diff against
    try:
        from . import gitgate
        await gitgate.ensure_repo(slug)
        await gitgate.run_git(slug, "add", "-A")
        await gitgate.run_git(slug, "commit", "-q", "-m", "project created")
    except Exception:  # noqa: BLE001 — a git hiccup must not block project creation
        pass
    return {"slug": slug, "name": body.name}


@router.get("/projects/{slug}")
async def get_project(slug: str):
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM projects WHERE slug = ?", (slug,)) as cur:
            row = await cur.fetchone()
        active = await get_active_project(db)
    finally:
        await db.close()
    if row is None:
        raise HTTPException(status_code=404, detail="no such project")
    return {**dict(row), "project_md": read_project_md(slug), "loaded": active == slug}


class SetAutonomy(BaseModel):
    level: str | None      # read_only | stage | gated | full  (None/full = unrestricted)


@router.put("/projects/{slug}/autonomy")
async def set_autonomy(slug: str, body: SetAutonomy):
    from . import autonomy
    if body.level not in (None, *autonomy.LEVELS):
        raise HTTPException(status_code=400, detail="invalid autonomy level")
    # store None for 'full' so the default stays unrestricted
    value = None if body.level in (None, "full") else body.level
    db = await get_db()
    try:
        cur = await db.execute("UPDATE projects SET autonomy = ? WHERE slug = ?",
                               (value, slug))
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="no such project")
    finally:
        await db.close()
    return {"ok": True, "autonomy": value or "full"}


@router.put("/projects/{slug}/md")
async def update_project_md(slug: str, body: UpdateProjectMd):
    if not project_md_path(slug).parent.exists():
        raise HTTPException(status_code=404, detail="no such project")
    project_md_path(slug).write_text(body.content)
    db = await get_db()
    try:
        await refresh_all_projects(db)
    finally:
        await db.close()
    return {"ok": True}


@router.post("/projects/{slug}/load")
async def load_project(slug: str):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM projects WHERE slug = ? AND deleted_at IS NULL", (slug,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such project")
        await set_state(db, "active_project", slug)
    finally:
        await db.close()
    return {"ok": True, "active": slug}


@router.delete("/projects/{slug}")
async def soft_delete_project(slug: str):
    """Move to the recently-deleted bin. Files stay on disk; restorable."""
    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM projects WHERE slug = ? AND deleted_at IS NULL", (slug,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such project")
        await db.execute(
            "UPDATE projects SET deleted_at = datetime('now') WHERE slug = ?", (slug,))
        if await get_active_project(db) == slug:
            await set_state(db, "active_project", None)
        await db.commit()
        await refresh_all_projects(db)
    finally:
        await db.close()
    return {"ok": True}


@router.post("/projects/{slug}/restore")
async def restore_project(slug: str):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM projects WHERE slug = ? AND deleted_at IS NOT NULL", (slug,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="not in the deleted bin")
        await db.execute(
            "UPDATE projects SET deleted_at = NULL WHERE slug = ?", (slug,))
        await db.commit()
        await refresh_all_projects(db)
    finally:
        await db.close()
    return {"ok": True}


@router.delete("/projects/{slug}/purge")
async def purge_project(slug: str):
    """Permanent: only allowed from the bin. Removes files and DB rows;
    conversations survive, detached from the project."""
    import shutil

    db = await get_db()
    try:
        async with db.execute(
            "SELECT id FROM projects WHERE slug = ? AND deleted_at IS NOT NULL", (slug,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=400,
                                detail="soft-delete first — purge only empties the bin")
        pid = row["id"]
        await db.execute("UPDATE conversations SET project_id = NULL WHERE project_id = ?", (pid,))
        await db.execute("DELETE FROM runs WHERE project_id = ?", (pid,))
        await db.execute("DELETE FROM projects WHERE id = ?", (pid,))
        await db.commit()
        await refresh_all_projects(db)
    finally:
        await db.close()
    project_path = settings.projects_dir / slug
    if project_path.exists():
        shutil.rmtree(project_path)
    return {"ok": True}


@router.post("/projects/unload")
async def unload_project():
    db = await get_db()
    try:
        await set_state(db, "active_project", None)
    finally:
        await db.close()
    return {"ok": True, "active": None}


@router.get("/debug/context")
async def debug_context():
    """The exact system prompt Jarvis gets this turn — for eyeballing context assembly."""
    db = await get_db()
    try:
        prompt = await assemble_system_prompt(db)
        active = await get_active_project(db)
    finally:
        await db.close()
    from .memory import estimate_tokens
    return {"active_project": active, "system_prompt": prompt,
            "tokens": estimate_tokens(prompt)}
