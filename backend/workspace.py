"""Project workspace: files, a light code runner, and a todo.md checklist.

Deliberately file-based — everything the GUI touches here is a plain file in
projects/<slug>/, so the agent can read and edit the same workspace with
tools later without a second data model.
"""
import io
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

import aiosqlite
from fastapi import APIRouter, Body, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .auth import require_user
from .config import settings
from .db import get_db
from .fsutil import SKIP_DIRS, list_tree, read_text_or_binary, safe_join
from .runner import run_python

router = APIRouter(prefix="/api/projects/{slug}", tags=["workspace"],
                   dependencies=[Depends(require_user)])


async def project_dir(slug: str) -> Path:
    db = await get_db()
    try:
        async with db.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such project")
    finally:
        await db.close()
    return settings.projects_dir / slug


def _refuse_git(base: Path, p: Path) -> Path:
    """Repo internals belong to the git gate — nothing writes into .git/."""
    if p.relative_to(base.resolve()).parts[:1] == (".git",):
        raise HTTPException(status_code=400, detail=".git is managed by the git gate")
    return p


class SaveFile(BaseModel):
    path: str
    content: str


class RunRequest(BaseModel):
    path: str | None = None    # run an existing file...
    code: str | None = None    # ...or scratch code (saved to code/scratch.py)


class TodoAction(BaseModel):
    action: str                # add | toggle | delete
    text: str | None = None
    index: int | None = None


@router.get("/files")
async def files(slug: str):
    return {"files": list_tree(await project_dir(slug))}


@router.get("/file")
async def read_file(slug: str, path: str):
    p = safe_join(await project_dir(slug), path)
    return {"path": path, **read_text_or_binary(p)}


@router.put("/file")
async def save_file(slug: str, body: SaveFile):
    base = await project_dir(slug)
    p = _refuse_git(base, safe_join(base, body.path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body.content)
    return {"ok": True, "path": body.path}


@router.delete("/file")
async def delete_file(slug: str, path: str):
    p = safe_join(await project_dir(slug), path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    if p.name == "project.md":
        raise HTTPException(status_code=400, detail="project.md is the project's journal")
    p.unlink()
    return {"ok": True}


@router.post("/upload")
async def upload(slug: str, file: UploadFile, dest: str = ""):
    base = await project_dir(slug)
    p = _refuse_git(base, safe_join(base, f"{dest.strip('/')}/{file.filename}".lstrip("/")))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(await file.read())
    return {"ok": True, "path": str(p.relative_to(base))}


def _zip_members(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    """Regular-file members that are safe to extract, as (info, relpath).
    Absolute paths, `..`, symlinks and junk dirs are silently skipped;
    a single GitHub-style top-level directory is stripped."""
    kept = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        mode = info.external_attr >> 16
        if stat.S_IFMT(mode) and not stat.S_ISREG(mode):
            continue  # symlink / device / anything non-regular
        name = info.filename.replace("\\", "/")
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            continue
        if any(part in SKIP_DIRS or part == ".git" for part in p.parts[:-1]):
            continue
        kept.append((info, str(p)))
    tops = {rel.split("/", 1)[0] for _, rel in kept}
    if len(tops) == 1 and all("/" in rel for _, rel in kept):
        kept = [(info, rel.split("/", 1)[1]) for info, rel in kept]
    return kept


@router.post("/upload_archive")
async def upload_archive(slug: str, file: UploadFile, dest: str = Form("code")):
    base = await project_dir(slug)
    dest_dir = _refuse_git(base, safe_join(base, dest.strip("/") or "code"))
    data = await file.read()
    if not (file.filename or "").lower().endswith(".zip") or not data.startswith(b"PK"):
        raise HTTPException(status_code=400, detail="only zip archives are accepted")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="not a valid zip archive")

    members = _zip_members(zf)
    max_bytes = settings.upload_max_uncompressed_mb * 1024 * 1024
    if len(members) > settings.upload_max_files:
        raise HTTPException(status_code=413,
                            detail=f"too many files ({len(members)} > {settings.upload_max_files})")
    if sum(info.file_size for info, _ in members) > max_bytes:
        raise HTTPException(status_code=413,
                            detail=f"archive exceeds {settings.upload_max_uncompressed_mb} MB uncompressed")

    written: list[Path] = []
    total = 0
    try:
        for info, rel in members:
            target = safe_join(dest_dir, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            written.append(target)  # before writing, so a mid-file abort cleans it up
            with zf.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(65536):
                    total += len(chunk)
                    if total > max_bytes:  # headers lied (zip bomb)
                        raise HTTPException(
                            status_code=413,
                            detail=f"archive exceeds {settings.upload_max_uncompressed_mb} MB uncompressed")
                    out.write(chunk)
    except HTTPException:
        for p in written:
            p.unlink(missing_ok=True)
        raise
    return {"files": len(written), "bytes": total,
            "dest": str(dest_dir.relative_to(base)) or "."}


@router.get("/raw/{path:path}")
async def raw(slug: str, path: str):
    p = safe_join(await project_dir(slug), path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    return FileResponse(p)


@router.post("/run")
async def run(slug: str, body: RunRequest):
    base = await project_dir(slug)
    if body.code is not None:
        rel = "code/scratch.py"
        p = safe_join(base, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body.code)
    elif body.path:
        rel = body.path
        p = safe_join(base, rel)
        if not p.is_file():
            raise HTTPException(status_code=404, detail="no such file")
        if p.suffix != ".py":
            raise HTTPException(status_code=400, detail="only .py files can be run")
    else:
        raise HTTPException(status_code=400, detail="give 'path' or 'code'")
    result = await run_python(base, rel)
    db = await get_db()
    try:
        async with db.execute("SELECT id FROM projects WHERE slug = ?", (slug,)) as cur:
            row = await cur.fetchone()
        await db.execute(
            "INSERT INTO runs (project_id, status) VALUES (?, ?)",
            (row["id"], "ok" if result["exit_code"] == 0 else "failed"),
        )
        await db.commit()
    finally:
        await db.close()
    return {"script": rel, **result}


# --- organizer: dirs, marks, moves ------------------------------------------
# A dir's "mark" is a .about.md inside it — a note telling Jarvis what belongs
# there ("anything pertaining to code goes here"). File-based so the
# organize_project skill can read the same scheme later.

class MoveRequest(BaseModel):
    src: str
    dest: str  # full new relative path, e.g. "images/plot.png"


class MkdirRequest(BaseModel):
    path: str
    mark: str | None = None


class MarkRequest(BaseModel):
    path: str  # "" = project root
    mark: str


def _read_mark(d: Path) -> str:
    about = d / ".about.md"
    return about.read_text().strip() if about.exists() else ""


@router.get("/dirs")
async def list_dirs(slug: str):
    base = await project_dir(slug)
    dirs = [{"path": "", "mark": _read_mark(base)}]
    for p in sorted(base.rglob("*")):
        if not p.is_dir():
            continue
        parts = p.relative_to(base).parts
        if any(part.startswith(".") or part in
               {"__pycache__", "node_modules", ".git"} for part in parts):
            continue
        dirs.append({"path": str(p.relative_to(base)), "mark": _read_mark(p)})
    return {"dirs": dirs}


@router.post("/mkdir")
async def mkdir(slug: str, body: MkdirRequest):
    base = await project_dir(slug)
    p = safe_join(base, body.path)
    p.mkdir(parents=True, exist_ok=True)
    if body.mark:
        (p / ".about.md").write_text(body.mark.strip() + "\n")
    return {"ok": True, "path": body.path}


@router.delete("/dirs")
async def rmdir(slug: str, path: str):
    base = await project_dir(slug)
    p = safe_join(base, path)
    if not path or not p.is_dir():
        raise HTTPException(status_code=404, detail="no such directory")
    contents = [c for c in p.iterdir() if c.name != ".about.md"]
    if contents:
        raise HTTPException(status_code=400, detail="directory not empty")
    (p / ".about.md").unlink(missing_ok=True)
    p.rmdir()
    return {"ok": True}


@router.put("/dirs/mark")
async def set_mark(slug: str, body: MarkRequest):
    base = await project_dir(slug)
    d = safe_join(base, body.path) if body.path else base
    if not d.is_dir():
        raise HTTPException(status_code=404, detail="no such directory")
    about = d / ".about.md"
    if body.mark.strip():
        about.write_text(body.mark.strip() + "\n")
    else:
        about.unlink(missing_ok=True)
    return {"ok": True}


@router.post("/move")
async def move_file(slug: str, body: MoveRequest):
    base = await project_dir(slug)
    src = _refuse_git(base, safe_join(base, body.src))
    dest = _refuse_git(base, safe_join(base, body.dest))
    if not src.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    if src.name == "project.md" and src.parent == base:
        raise HTTPException(status_code=400, detail="project.md stays at the project root")
    if dest.exists():
        raise HTTPException(status_code=409, detail="destination already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)
    return {"ok": True, "src": body.src, "dest": body.dest}


# --- context files: which project files are loaded into Jarvis's context ----
# Selection lives in projects/<slug>/.context.json (a list of relative paths).
# assemble_system_prompt reads it when the project is active. Token counts are
# a cheap chars/4 estimate — enough to budget, not exact.

from .memory import context_selection, set_context_selection, estimate_tokens  # noqa: E402


@router.get("/context")
async def get_context(slug: str):
    base = await project_dir(slug)
    selected = set(context_selection(slug))
    files = []
    total = 0
    for f in list_tree(base):
        path = f["path"]
        info = read_text_or_binary(base / path)
        tokens = 0 if info["binary"] else estimate_tokens(info["content"])
        is_sel = path in selected
        if is_sel:
            total += tokens
        files.append({"path": path, "tokens": tokens,
                      "binary": info["binary"], "selected": is_sel})
    return {"files": files, "selected_tokens": total}


class ContextSelection(BaseModel):
    files: list[str]


@router.put("/context")
async def put_context(slug: str, body: ContextSelection):
    base = await project_dir(slug)
    valid = {f["path"] for f in list_tree(base)}
    chosen = [p for p in body.files if p in valid]
    set_context_selection(slug, chosen)
    return {"ok": True, "files": chosen}


# --- control-board layout (persisted per project, hidden from file views) ----

@router.get("/layout")
async def get_layout(slug: str):
    p = (await project_dir(slug)) / ".workspace.json"
    if not p.exists():
        return {"layout": None}
    return {"layout": json.loads(p.read_text())}


@router.put("/layout")
async def save_layout(slug: str, layout: dict = Body(...)):
    p = (await project_dir(slug)) / ".workspace.json"
    p.write_text(json.dumps(layout))
    return {"ok": True}


# --- todo.md checklist ------------------------------------------------------
# helpers extracted to a pure module so the guest can run todo_update in-guest
from .agent.tools.todostore import _parse_todos, _todo_path, _write_todos  # noqa: F401,E402


@router.get("/todos")
async def get_todos(slug: str):
    return {"todos": _parse_todos(await project_dir(slug))}


@router.post("/todos")
async def modify_todos(slug: str, body: TodoAction):
    base = await project_dir(slug)
    todos = _parse_todos(base)
    if body.action == "add" and body.text:
        todos.append({"done": False, "text": body.text.strip()})
    elif body.action == "toggle" and body.index is not None and 0 <= body.index < len(todos):
        todos[body.index]["done"] = not todos[body.index]["done"]
    elif body.action == "delete" and body.index is not None and 0 <= body.index < len(todos):
        todos.pop(body.index)
    else:
        raise HTTPException(status_code=400, detail="bad todo action")
    _write_todos(base, todos)
    return {"todos": todos}


# --- staged changes (Jarvis's pending edits) ---------------------------------
# Staged content is quarantined: these endpoints return it as text for diff
# display only; nothing serves, runs or imports it until approved.

from . import staging as _staging  # noqa: E402


class StagingAction(BaseModel):
    paths: list[str] | None = None   # None = everything


@router.get("/staging")
async def staged_list(slug: str):
    await project_dir(slug)
    return {"staged": _staging.list_staged(slug)}


@router.get("/staging/diff")
async def staged_diff(slug: str, path: str):
    base = await project_dir(slug)
    staged = safe_join(base / _staging.STAGING, path)
    if not staged.is_file():
        raise HTTPException(status_code=404, detail="nothing staged at that path")
    canonical = safe_join(base, path)

    def _text(p: Path) -> str | None:
        if not p.is_file():
            return None
        try:
            return p.read_text()
        except UnicodeDecodeError:
            return f"(binary, {p.stat().st_size} bytes)"

    return {"path": path, "old": _text(canonical), "new": _text(staged)}


@router.post("/staging/approve")
async def staged_approve(slug: str, body: StagingAction):
    await project_dir(slug)
    return {"applied": _staging.approve(slug, body.paths)}


@router.post("/staging/reject")
async def staged_reject(slug: str, body: StagingAction):
    await project_dir(slug)
    return {"rejected": _staging.reject(slug, body.paths)}
