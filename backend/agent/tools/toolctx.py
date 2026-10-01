"""Shared helpers for tool handlers."""
from ...config import settings
from ...db import get_db
from ...memory import get_active_project


class NoProjectError(LookupError):
    """No usable project for a file tool. `for_model`: the message is written for
    the model, so argcheck.crash_message hands it back as is instead of calling
    it a harness fault."""
    for_model = True


async def active_slug() -> str | None:
    """The running operation's pinned project when inside one (set per turn by
    chat/agent/schedule from the conversation's binding — this is what keeps
    concurrent turns in different projects apart), else the GUI's global
    active project."""
    from ... import runtime
    pinned = runtime.active_project.get()
    if pinned is not runtime.ACTIVE_UNSET:
        return pinned
    db = await get_db()
    try:
        return await get_active_project(db)
    finally:
        await db.close()


async def _ensure_artifact_project(slug: str) -> None:
    """Lazily create the hidden per-chat artifact project the first time a
    file tool touches it. Idempotent. The `.artifact` marker distinguishes
    these hidden stores from real projects in listings."""
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


async def adopt_artifact_store(slug: str) -> bool:
    """After a project-less GUEST turn: the turn's workspace was the chat's
    artifact store (chat.py passes chat-<id> as the guest's active slug), and
    the guest's writes landed in it at turn end without any of the bookkeeping
    the host-loop fallback did on first use. Register it (hidden project row,
    project.md, marker) once it holds a file, so /artifacts lists it. A chat
    that wrote nothing leaves nothing behind. Idempotent."""
    from ...fsutil import list_tree
    project_dir = settings.projects_dir / slug
    if not any(f["path"] != "project.md" for f in list_tree(project_dir)):
        return False
    await _ensure_artifact_project(slug)
    return True


async def trashed_pin() -> tuple[str, str] | None:
    """(slug, name) of the project this chat is pinned to when that project is in
    Recently deleted, else None. The turn setup drops a trashed pin, so the chat
    would look project-less to its tools; this is how they tell the difference."""
    from ... import runtime
    cid = runtime.conversation_id.get()
    if not cid:
        return None
    db = await get_db()
    try:
        async with db.execute(
                "SELECT p.slug, p.name FROM conversations c "
                "JOIN projects p ON p.id = c.project_id "
                "WHERE c.id = ? AND p.deleted_at IS NOT NULL", (cid,)) as cur:
            r = await cur.fetchone()
    finally:
        await db.close()
    return (r["slug"], r["name"]) if r else None


def trashed_pin_message(slug: str, name: str) -> str:
    return (f"this chat's project '{slug}' ({name}) is in the trash (Recently deleted), "
            "so it has no workspace. Do not switch to another project on your own. "
            "Tell the operator the project was deleted and offer to restore it "
            "(Projects > Recently deleted > Restore) or to move this chat to another "
            "project.")


async def require_project() -> str:
    from ... import runtime
    slug = await active_slug()
    if not slug:
        trashed = await trashed_pin()
        if trashed:
            raise NoProjectError(trashed_pin_message(*trashed))
        # project-less chat: file tools land in the conversation's hidden
        # artifact store instead of erroring
        artifact = runtime.artifact_slug.get()
        if artifact:
            await _ensure_artifact_project(artifact)
            return artifact
        raise NoProjectError(
            "no project is loaded — call load_project first "
            "(project slugs are listed in your 'All projects' context)")
    if not (settings.projects_dir / slug / "project.md").exists():
        raise NoProjectError(
            f"active project '{slug}' has no files on disk — "
            "call load_project with a different slug, or ask the operator to restore it")
    return slug


async def web_session() -> str:
    """Fetch-ledger scope for web tools: the running operation's key (set per
    chat turn / agent run / funnel job), falling back to the project slug for
    any caller outside an operation. The old project-slug-only keying made
    claims permanent — a scheduled run could never re-read a page an earlier
    turn in the same project had already fetched."""
    from ... import runtime
    op = runtime.web_session.get()
    if op:
        return op
    return (await active_slug()) or "global"
