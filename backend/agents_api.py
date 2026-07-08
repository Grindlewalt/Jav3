"""Agent definitions: agents/<slug>/AGENT.md — frontmatter + system prompt.

The exclusion model is deliberate: an agent gets EVERYTHING (context files,
tools, skills) by default and lists what to remove, so necessary pieces
can't be forgotten — only knowingly taken away. These defs are authoring
only for now; the spawn tool that runs them lands with the tool layer.
"""
import re

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .agent.tools.registry import _parse_md
from .auth import require_user
from .config import settings

router = APIRouter(prefix="/api/agents", tags=["agents"],
                   dependencies=[Depends(require_user)])

DEFAULT_PROMPT = """You are {name}, a focused agent working for the operator.
Stay on brief, report back concisely, and say so when something is outside
your scope.
"""

FIELD_DEFAULTS = {
    "description": "",
    "model": "",          # "" = inherit the main model (deepseek-v4-flash)
    "base_url": "",       # "" = default DeepSeek endpoint; e.g. ollama: http://localhost:11434/v1
    "own_memory": False,  # experimental: agent keeps its own notes instead of sharing
    "context_exclude": [],
    "tools_exclude": [],
    "skills_exclude": [],
    # headless runs (spawn_agent, schedules) get the tight subagent iteration
    # cap by default; set this to grant a specific agent more rounds. 0 = default.
    "max_iterations": 0,
}


class CreateAgent(BaseModel):
    name: str


class SaveAgent(BaseModel):
    name: str
    description: str = ""
    model: str = ""
    base_url: str = ""
    own_memory: bool = False
    context_exclude: list[str] = []
    tools_exclude: list[str] = []
    skills_exclude: list[str] = []
    max_iterations: int = 0
    prompt: str = ""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise HTTPException(status_code=400, detail="name produces empty slug")
    return slug


def _agent_path(slug: str):
    return settings.agents_dir / slug / "AGENT.md"


def _write(slug: str, body: SaveAgent) -> None:
    meta = {"name": body.name, **{k: getattr(body, k) for k in FIELD_DEFAULTS}}
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    path = _agent_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n\n{body.prompt.strip()}\n")


def _read(slug: str) -> dict:
    path = _agent_path(slug)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such agent")
    meta = _parse_md(path)
    if meta is None:
        raise HTTPException(status_code=500, detail="unparseable AGENT.md")
    out = {"slug": slug, "name": meta.get("name", slug), "prompt": meta.get("body", "")}
    for key, default in FIELD_DEFAULTS.items():
        out[key] = meta.get(key, default)
    return out


def _list_dir(base):
    agents = []
    if base.exists():
        for md in sorted(base.glob("*/AGENT.md")):
            if md.parent.name.startswith("."):
                continue  # skip the .trash bin
            meta = _parse_md(md) or {}
            agents.append({
                "slug": md.parent.name,
                "name": meta.get("name", md.parent.name),
                "description": meta.get("description", ""),
                "model": meta.get("model", ""),
            })
    return agents


@router.get("")
async def list_agents():
    return {"agents": _list_dir(settings.agents_dir)}


@router.get("/trash")
async def list_trash():
    return {"agents": _list_dir(settings.agents_dir / ".trash")}


@router.post("")
async def create_agent(body: CreateAgent):
    slug = _slugify(body.name)
    if _agent_path(slug).exists():
        raise HTTPException(status_code=409, detail=f"agent '{slug}' already exists")
    _write(slug, SaveAgent(name=body.name, prompt=DEFAULT_PROMPT.format(name=body.name)))
    return {"slug": slug}


@router.get("/{slug}")
async def read_agent(slug: str):
    return _read(slug)


@router.put("/{slug}")
async def save_agent(slug: str, body: SaveAgent):
    if not _agent_path(slug).is_file():
        raise HTTPException(status_code=404, detail="no such agent")
    _write(slug, body)
    return {"ok": True}


@router.delete("/{slug}")
async def delete_agent(slug: str):
    """Soft delete: move to the .trash bin. Restorable until purged."""
    import shutil
    path = _agent_path(slug)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such agent")
    trash = settings.agents_dir / ".trash"
    trash.mkdir(exist_ok=True)
    dest = trash / slug
    if dest.exists():
        shutil.rmtree(dest)  # a re-created-then-deleted agent overwrites its old grave
    shutil.move(str(path.parent), str(dest))
    return {"ok": True}


@router.post("/{slug}/restore")
async def restore_agent(slug: str):
    import shutil
    src = settings.agents_dir / ".trash" / slug
    if not (src / "AGENT.md").is_file():
        raise HTTPException(status_code=404, detail="not in the trash")
    dest = settings.agents_dir / slug
    if dest.exists():
        raise HTTPException(status_code=409,
                            detail=f"an agent '{slug}' already exists — rename it first")
    shutil.move(str(src), str(dest))
    return {"ok": True}


@router.delete("/{slug}/purge")
async def purge_agent(slug: str):
    """Permanent: only from the trash."""
    import shutil
    src = settings.agents_dir / ".trash" / slug
    if not (src / "AGENT.md").is_file():
        raise HTTPException(status_code=400, detail="delete first — purge only empties trash")
    shutil.rmtree(src)
    return {"ok": True}
