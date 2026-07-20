"""GUI access to Jarvis's memory files: list, read, edit, create notes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .config import settings
from .fsutil import list_tree, read_text_or_binary, safe_join
from .memory import (ensure_memory_seeds, estimate_tokens, note_description,
                     note_taint, note_trusted, notes_dir, parse_note, promote_note)

router = APIRouter(prefix="/api/memory", tags=["memory"],
                   dependencies=[Depends(require_user)])

# Regenerated from project summaries — hand edits get overwritten.
AUTO_GENERATED = {"all-projects.md"}


class SaveFile(BaseModel):
    path: str
    content: str


@router.get("")
async def list_memory():
    ensure_memory_seeds()
    files = list_tree(settings.memory_dir)
    for f in files:
        f["auto_generated"] = f["path"] in AUTO_GENERATED
        try:  # ≈input-token cost of the file if it rides the context
            f["tokens"] = estimate_tokens(
                (settings.memory_dir / f["path"]).read_text())
        except (UnicodeDecodeError, OSError):
            f["tokens"] = None
    return {"files": files}


@router.get("/file")
async def read_memory(path: str):
    p = safe_join(settings.memory_dir, path)
    return {"path": path, **read_text_or_binary(p)}


@router.put("/file")
async def save_memory(body: SaveFile):
    p = safe_join(settings.memory_dir, body.path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body.content)
    return {"ok": True, "path": body.path}


@router.get("/notes")
async def list_notes():
    """Notes with their trust/taint metadata — the Context page uses this to
    badge agent-written and web/research-tainted notes and offer 'Promote'."""
    nd = notes_dir()
    out = []
    if nd.exists():
        for p in sorted(nd.glob("*.md")):
            try:
                meta, body = parse_note(p.read_text())
            except OSError:
                continue
            out.append({"name": p.stem,
                        "description": note_description(meta, body),
                        "source": str(meta.get("source", "operator")),
                        "approved": bool(meta.get("approved")),
                        "taint": note_taint(meta),
                        "trusted": note_trusted(meta)})
    return {"notes": out}


@router.post("/notes/{name}/promote")
async def promote(name: str):
    """Operator clears an agent/tainted note into trusted binding context."""
    if not promote_note(name):
        raise HTTPException(status_code=404, detail="no such note")
    return {"ok": True, "name": name}
