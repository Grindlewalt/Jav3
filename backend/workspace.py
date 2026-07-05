"""Project workspace: files, a light run sandbox, and a todo.md checklist.

Deliberately file-based — everything the GUI touches here is a plain file in
projects/<slug>/, so the agent can read and edit the same workspace with
tools later without a second data model.
"""
import json
import re
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Body, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .auth import require_user
from .config import settings
from .db import get_db
from .fsutil import list_tree, read_text_or_binary, safe_join
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
    p = safe_join(await project_dir(slug), body.path)
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
    p = safe_join(base, f"{dest.strip('/')}/{file.filename}".lstrip("/"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(await file.read())
    return {"ok": True, "path": str(p.relative_to(base))}


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
    src = safe_join(base, body.src)
    dest = safe_join(base, body.dest)
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

TODO_RE = re.compile(r"^- \[([ x])\] (.*)$")


def _todo_path(base: Path) -> Path:
    return base / "todo.md"


def _parse_todos(base: Path) -> list[dict]:
    path = _todo_path(base)
    if not path.exists():
        return []
    todos = []
    for line in path.read_text().splitlines():
        m = TODO_RE.match(line.strip())
        if m:
            todos.append({"done": m.group(1) == "x", "text": m.group(2)})
    return todos


def _write_todos(base: Path, todos: list[dict]) -> None:
    lines = ["# Todo", ""]
    lines += [f"- [{'x' if t['done'] else ' '}] {t['text']}" for t in todos]
    _todo_path(base).write_text("\n".join(lines) + "\n")


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
