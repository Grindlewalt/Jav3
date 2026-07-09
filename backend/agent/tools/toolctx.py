"""Shared helpers for tool handlers."""
from ...config import settings
from ...db import get_db
from ...memory import get_active_project


async def active_slug() -> str | None:
    db = await get_db()
    try:
        return await get_active_project(db)
    finally:
        await db.close()


async def require_project() -> str:
    slug = await active_slug()
    if not slug:
        raise LookupError(
            "no project is loaded — call load_project first "
            "(project slugs are listed in your 'All projects' context)")
    if not (settings.projects_dir / slug / "project.md").exists():
        raise LookupError(
            f"active project '{slug}' has no files on disk — "
            "call load_project with a different slug, or ask the operator to restore it")
    return slug
