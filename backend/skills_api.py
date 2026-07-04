"""Skill authoring (for the operator) + the tool catalogue.

A skill is skills/<slug>/SKILL.md — markdown with YAML frontmatter, same
format the registry compiles. New skills start with `enabled: false`, so
they're catalogued but not granted to the model until flipped.
"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .agent.tools.registry import compile_registry, load_registry, _parse_md
from .auth import require_user
from .config import settings

router = APIRouter(prefix="/api", tags=["skills"], dependencies=[Depends(require_user)])

SKILL_TEMPLATE = """---
name: {slug}
description: {description}
when_to_use: (fill in — the loop reads this at selection time)
enabled: false
parameters:
  type: object
  properties: {{}}
---

TODO: write this skill. References, examples, and API notes go here in the
body — injected into context when the skill is selected.
"""


class CreateSkill(BaseModel):
    name: str
    description: str = "(describe what this skill does)"


class SaveSkill(BaseModel):
    content: str


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise HTTPException(status_code=400, detail="name produces empty slug")
    return slug


@router.get("/skills")
async def list_skills():
    skills = []
    if settings.skills_dir.exists():
        for md in sorted(settings.skills_dir.glob("*/SKILL.md")):
            meta = _parse_md(md) or {}
            skills.append({
                "slug": md.parent.name,
                "name": meta.get("name", md.parent.name),
                "description": meta.get("description", ""),
                "enabled": meta.get("enabled", True) is not False,
            })
    return {"skills": skills}


@router.post("/skills")
async def create_skill(body: CreateSkill):
    slug = _slugify(body.name)
    path = settings.skills_dir / slug / "SKILL.md"
    if path.exists():
        raise HTTPException(status_code=409, detail=f"skill '{slug}' already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKILL_TEMPLATE.format(slug=slug, description=body.description))
    compile_registry()
    return {"slug": slug}


@router.get("/skills/{slug}")
async def read_skill(slug: str):
    path = settings.skills_dir / slug / "SKILL.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such skill")
    return {"slug": slug, "content": path.read_text()}


@router.put("/skills/{slug}")
async def save_skill(slug: str, body: SaveSkill):
    path = settings.skills_dir / slug / "SKILL.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such skill")
    path.write_text(body.content)
    compile_registry()
    return {"ok": True}


@router.get("/tools")
async def list_tools():
    """Everything in the registry, granted or not — the Tools tab reads this."""
    entries = load_registry()
    return {"tools": [{
        "name": e["name"],
        "description": e.get("description", ""),
        "when_to_use": e.get("when_to_use", ""),
        "enabled": e.get("enabled", True) is not False,
        "source": e.get("source", ""),
    } for e in entries]}
