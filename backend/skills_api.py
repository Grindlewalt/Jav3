"""Skill authoring (for the operator) + the tool catalogue.

A skill is skills/<slug>/SKILL.md — markdown with YAML frontmatter, same
format the registry compiles. The GUI edits skills as structured FIELDS and
the frontmatter is serialized server-side (always-valid YAML); raw-content
editing stays available as the advanced path. New skills are granted by
default (operator decision 2026-07-09) — untick to catalogue without granting.
"""
import re

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .agent.tools.registry import compile_registry, load_registry, _parse_md
from .auth import require_user
from .config import settings

router = APIRouter(prefix="/api", tags=["skills"], dependencies=[Depends(require_user)])


class CreateSkill(BaseModel):
    name: str
    description: str = "(describe what this skill does)"


class SaveSkill(BaseModel):
    content: str


class SkillFields(BaseModel):
    """Structured skill definition — what the form-based editor speaks."""
    description: str
    when_to_use: str = ""
    enabled: bool = True
    body: str = ""
    # [{name, type, description, required}] -> JSON-schema parameters
    params: list[dict] = []


def _serialize(slug: str, f: SkillFields) -> str:
    props, required = {}, []
    for p in f.params:
        name = str(p.get("name", "")).strip()
        if not name:
            continue
        props[name] = {"type": p.get("type") or "string",
                       "description": p.get("description", "")}
        if p.get("required"):
            required.append(name)
    meta = {"name": slug, "description": f.description,
            "when_to_use": f.when_to_use, "enabled": f.enabled,
            "parameters": {"type": "object", "properties": props,
                           **({"required": required} if required else {})}}
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True,
                        default_flow_style=False).strip()
    return f"---\n{fm}\n---\n\n{f.body.strip()}\n"


def _fields(md_path) -> dict:
    meta = _parse_md(md_path) or {}
    props = (meta.get("parameters") or {}).get("properties") or {}
    required = set((meta.get("parameters") or {}).get("required") or [])
    return {
        "description": meta.get("description", ""),
        "when_to_use": meta.get("when_to_use", ""),
        "enabled": meta.get("enabled", True) is not False,
        "body": meta.get("body", ""),
        "params": [{"name": n, "type": p.get("type", "string"),
                    "description": p.get("description", ""),
                    "required": n in required} for n, p in props.items()],
    }


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
    path.write_text(_serialize(slug, SkillFields(
        description=body.description,
        when_to_use="(fill in — the model reads this when picking tools)",
        body="(instructions the model follows when it invokes this skill)")))
    compile_registry()
    return {"slug": slug}


@router.get("/skills/{slug}")
async def read_skill(slug: str):
    path = settings.skills_dir / slug / "SKILL.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such skill")
    return {"slug": slug, "content": path.read_text(), "fields": _fields(path)}


@router.put("/skills/{slug}")
async def save_skill(slug: str, body: SaveSkill):
    """Raw-content save (the advanced editor path)."""
    path = settings.skills_dir / slug / "SKILL.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such skill")
    path.write_text(body.content)
    compile_registry()
    return {"ok": True}


@router.put("/skills/{slug}/fields")
async def save_skill_fields(slug: str, body: SkillFields):
    """Form save: fields in, valid frontmatter out — no hand-written YAML."""
    path = settings.skills_dir / slug / "SKILL.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such skill")
    if not body.description.strip():
        raise HTTPException(status_code=400, detail="description is required")
    path.write_text(_serialize(slug, body))
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
