"""Artifacts: files Jarvis made in project-less chats.

Each such chat gets a hidden per-conversation project (`chat-<id>`, created
lazily by toolctx when a file tool first runs). This API is the operator's
view over those stores: list/search them, edit files (the normal project file
endpoints work — a hidden slug is still a slug), and graduate them — convert
the store into a real visible project, or merge its files into an existing
project's files directly (writes are live; git is the undo surface).
"""
import shutil

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .config import settings
from .db import get_db
from .fsutil import list_tree
from .memory import refresh_all_projects
from .writes import apply_write

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"],
                   dependencies=[Depends(require_user)])


class Convert(BaseModel):
    name: str


class Merge(BaseModel):
    target: str  # visible project slug


async def _artifact_rows(db):
    async with db.execute(
        "SELECT slug, name, created_at FROM projects "
        "WHERE is_hidden = 1 AND deleted_at IS NULL ORDER BY created_at DESC"
    ) as cur:
        return await cur.fetchall()


def _files(slug: str) -> list[dict]:
    return [f for f in list_tree(settings.projects_dir / slug)
            if f["path"] != "project.md"]


@router.get("")
async def list_artifacts(q: str = ""):
    """All artifact stores with their files. `q` filters by filename or file
    content (these stores are small — a linear scan is fine)."""
    db = await get_db()
    try:
        rows = await _artifact_rows(db)
        out = []
        needle = q.strip().lower()
        for r in rows:
            cid = r["slug"].removeprefix("chat-")
            title = None
            if cid.isdigit():
                async with db.execute(
                    "SELECT summary FROM conversations WHERE id = ?", (int(cid),)
                ) as cur:
                    conv = await cur.fetchone()
                title = conv["summary"] if conv else None
            files = _files(r["slug"])
            if needle:
                def matches(f):
                    if needle in f["path"].lower():
                        return True
                    p = settings.projects_dir / r["slug"] / f["path"]
                    try:
                        return needle in p.read_text().lower()
                    except (UnicodeDecodeError, OSError):
                        return False
                files = [f for f in files if matches(f)]
                if not files:
                    continue
            if not files and not needle:
                continue   # empty store — nothing worth showing
            out.append({"slug": r["slug"], "chat_id": int(cid) if cid.isdigit() else None,
                        "title": title or f"chat #{cid}",
                        "created_at": r["created_at"], "files": files})
    finally:
        await db.close()
    return {"artifacts": out}


@router.post("/{slug}/convert")
async def convert_artifact(slug: str, body: Convert):
    """Graduate the store into a real, visible project. From here on its
    writes apply live like any other project's."""
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM projects WHERE slug = ? AND is_hidden = 1", (slug,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such artifact store")
        await db.execute(
            "UPDATE projects SET is_hidden = 0, name = ? WHERE slug = ?",
            (body.name.strip(), slug))
        await db.commit()
        (settings.projects_dir / slug / ".artifact").unlink(missing_ok=True)
        await refresh_all_projects(db)
    finally:
        await db.close()
    # artifact stores are minted repo-less; a real project needs the baseline
    # commit (git is the review/undo surface for live writes) — same block as
    # create_project, same best-effort stance
    try:
        from . import gitgate
        await gitgate.ensure_repo(slug)
        await gitgate.run_git(slug, "add", "-A")
        await gitgate.run_git(slug, "commit", "-q", "-m", "project created")
    except Exception:  # noqa: BLE001 — a git hiccup must not block the convert
        pass
    return {"ok": True, "slug": slug, "name": body.name.strip()}


@router.post("/{slug}/merge")
async def merge_artifact(slug: str, body: Merge):
    """Copy the store's files into the target project directly — the
    operator reviews them like any agent edit before they go canonical."""
    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM projects WHERE slug = ? AND is_hidden = 1", (slug,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such artifact store")
        async with db.execute(
            "SELECT 1 FROM projects WHERE slug = ? AND is_hidden = 0 "
            "AND deleted_at IS NULL", (body.target,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such target project")
    finally:
        await db.close()
    merged = []
    for f in _files(slug):
        data = (settings.projects_dir / slug / f["path"]).read_bytes()
        await apply_write(body.target, f["path"], data)
        merged.append(f["path"])
    return {"ok": True, "merged": merged, "target": body.target}


@router.delete("/{slug}")
async def delete_artifact(slug: str):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM projects WHERE slug = ? AND is_hidden = 1", (slug,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such artifact store")
        await db.execute(
            "UPDATE projects SET deleted_at = datetime('now') WHERE slug = ?", (slug,))
        await db.commit()
    finally:
        await db.close()
    import asyncio
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: shutil.rmtree(settings.projects_dir / slug, ignore_errors=True))
    return {"ok": True}
