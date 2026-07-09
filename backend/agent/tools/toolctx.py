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


async def _ensure_artifact_project(slug: str) -> None:
    """Lazily create the hidden per-chat artifact project the first time a
    file tool touches it. Idempotent. The `.artifact` marker is what tells
    staging to auto-approve writes (chat outputs never execute anywhere;
    approval friction returns when the store is converted/merged)."""
    project_dir = settings.projects_dir / slug
    if not (project_dir / "project.md").exists():
        project_dir.mkdir(parents=True, exist_ok=True)
        cid = slug.removeprefix("chat-")
        (project_dir / "project.md").write_text(
            f"# Chat artifacts\n\n## Summary\nFiles created in chat #{cid} "
            "(no project was loaded).\n")
        (project_dir / ".artifact").write_text("")
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO projects (slug, name, path, is_hidden) "
            "VALUES (?, ?, ?, 1)",
            (slug, f"Chat artifacts #{slug.removeprefix('chat-')}",
             str(project_dir)))
        await db.commit()
    finally:
        await db.close()


async def require_project() -> str:
    from ... import runtime
    slug = await active_slug()
    if not slug:
        # project-less chat: file tools land in the conversation's hidden
        # artifact store instead of erroring
        artifact = runtime.artifact_slug.get()
        if artifact:
            await _ensure_artifact_project(artifact)
            return artifact
        raise LookupError(
            "no project is loaded — call load_project first "
            "(project slugs are listed in your 'All projects' context)")
    if not (settings.projects_dir / slug / "project.md").exists():
        raise LookupError(
            f"active project '{slug}' has no files on disk — "
            "call load_project with a different slug, or ask the operator to restore it")
    return slug
