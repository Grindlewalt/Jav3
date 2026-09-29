"""GUI access to Jav3's memory files: list, read, edit, create notes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .config import settings
from .fsutil import list_tree, read_text_or_binary, safe_join
from .memory import (audit, ensure_memory_seeds, estimate_tokens, list_trash,
                     note_description, note_taint, note_trusted, notes_dir, parse_note,
                     promote_note, restore_trash, trash_note)

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
    """Notes with their trust/taint metadata — the Memory page uses this to
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


def _check_name(name: str) -> str:
    """A note name from a URL: one path segment, not hidden. The trash and the
    proposals live in dot-directories and are reached only through their own
    routes."""
    if (not name or name != name.strip() or name.startswith(".")
            or "/" in name or "\\" in name or "\x00" in name):
        raise HTTPException(status_code=400, detail="bad note name")
    return name


@router.delete("/notes/{name}")
async def delete_note(name: str):
    """Operator deletes a note. It goes to the trash, not away: restore it from
    GET /trash. (Agents delete the same way, with an audit event.)"""
    name = _check_name(name)
    try:
        tid = trash_note(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no such note") from None
    await audit("memory_deleted", "info", f"memory note '{name}' deleted by the operator",
                {"note": name, "by": "operator", "trash_id": tid})
    return {"ok": True, "name": name, "trash_id": tid}


@router.get("/trash")
async def trash():
    return {"items": list_trash()}


@router.post("/trash/{tid}/restore")
async def restore(tid: str):
    try:
        name = restore_trash(tid)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad trash id") from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not in the trash") from None
    except FileExistsError:
        raise HTTPException(
            status_code=409,
            detail="a note with that name exists now; rename or delete it first") from None
    return {"ok": True, "name": name}
