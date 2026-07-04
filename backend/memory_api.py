"""GUI access to Jarvis's memory files: list, read, edit, create notes."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .auth import require_user
from .config import settings
from .fsutil import list_tree, read_text_or_binary, safe_join
from .memory import ensure_memory_seeds

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
