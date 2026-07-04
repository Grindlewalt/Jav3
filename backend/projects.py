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
            "SELECT id, slug, name, github_remote, created_at FROM projects ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        active = await get_active_project(db)
    finally:
        await db.close()
    return {"projects": [dict(r) for r in rows], "active": active}


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
        async with db.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such project")
        await set_state(db, "active_project", slug)
    finally:
        await db.close()
    return {"ok": True, "active": slug}


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
    return {"active_project": active, "system_prompt": prompt}
