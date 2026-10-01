"""Skill authoring (for the operator) + the tool catalogue.

A skill is skills/<slug>/SKILL.md — markdown with YAML frontmatter, same
format the registry compiles. The GUI edits skills as structured FIELDS and
the frontmatter is serialized server-side (always-valid YAML); raw-content
editing stays available as the advanced path. New skills are granted by
default (operator decision 2026-07-09) — untick to catalogue without granting.
"""
import asyncio
import re

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import toolcatalog
from .agent.tools import imported
from .agent.tools.registry import compile_registry, load_registry, _parse_md
from .auth import require_user
from .config import settings

router = APIRouter(prefix="/api", tags=["skills"], dependencies=[Depends(require_user)])


class CreateSkill(BaseModel):
    name: str
    description: str = "(describe what this skill does)"


class SaveSkill(BaseModel):
    content: str


class ImportSkill(BaseModel):
    source: str
    name: str | None = None
    replace: bool = False


class Grant(BaseModel):
    granted: bool


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
            if imported.is_imported(md.parent, meta):
                e = imported.entry({**meta, "source": str(md)}, md.parent)
                skills.append({"slug": md.parent.name, "name": e["name"],
                               "description": e["description"],
                               "enabled": imported.offerable(e), "imported": True})
                continue
            skills.append({
                "slug": md.parent.name,
                "name": meta.get("name", md.parent.name),
                "description": meta.get("description", ""),
                "enabled": meta.get("enabled", True) is not False,
                "imported": False,
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


@router.post("/skills/import")
async def import_skill(body: ImportSkill):
    """Vendor an OpenClaw skill (folder / https git URL / clawhub:slug) as a
    pinned, ungranted snapshot. Operator cookie only — the guest has no path
    here, and the fetch runs host-side behind the SSRF guard."""
    from .skillimport import SkillImportError, import_skill as do_import
    try:
        return await asyncio.to_thread(do_import, body.source, body.name or None,
                                       body.replace)
    except SkillImportError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/skills/{slug}")
async def read_skill(slug: str):
    path = settings.skills_dir / slug / "SKILL.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such skill")
    return {"slug": slug, "content": path.read_text(), "fields": _fields(path)}


def _editable(slug: str):
    path = settings.skills_dir / slug / "SKILL.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such skill")
    if imported.is_imported(path.parent, _parse_md(path)):
        # an edit would break the pin and disable it; re-import to change it
        raise HTTPException(status_code=409,
                            detail="imported skills are pinned; re-import to change one")
    return path


@router.put("/skills/{slug}")
async def save_skill(slug: str, body: SaveSkill):
    """Raw-content save (the advanced editor path)."""
    path = _editable(slug)
    path.write_text(body.content)
    compile_registry()
    return {"ok": True}


@router.put("/skills/{slug}/fields")
async def save_skill_fields(slug: str, body: SkillFields):
    """Form save: fields in, valid frontmatter out — no hand-written YAML."""
    path = _editable(slug)
    if not body.description.strip():
        raise HTTPException(status_code=400, detail="description is required")
    path.write_text(_serialize(slug, body))
    compile_registry()
    return {"ok": True}


@router.put("/skills/{slug}/grant")
async def grant_skill(slug: str, body: Grant):
    """Grant or revoke an imported skill. Operator cookie only (this router's
    require_user); a grant is refused while any requirement is unmet or the pin
    fails, so the switch the Tools page locks is locked here too."""
    path = settings.skills_dir / slug / "SKILL.md"
    meta = _parse_md(path) if path.is_file() else None
    if meta is None or not imported.is_imported(path.parent, meta):
        raise HTTPException(status_code=404, detail="no such imported skill")
    if body.granted:
        e = imported.entry(meta, path.parent)
        unmet = [r["reason"] for r in e["requirements"] if not r["met"]]
        if e["blocked"] or unmet:
            raise HTTPException(status_code=409, detail=e["blocked"] or unmet[0])
    imported.set_grant(path.parent, body.granted)
    compile_registry()
    return {"ok": True, "granted": body.granted}


def _group(e: dict) -> str:
    if e.get("origin") == imported.ORIGIN:
        return "imported"
    return "yours" if e.get("kind") == "skill" else "builtin"


def _why_not(e: dict) -> str:
    """Why a built-in tool is not offered to the model right now, as a phrase
    ('' = it is). The same gates as registry._requirements_met, with words."""
    if e.get("enabled", True) is False:
        return "switched off in its tool folder: the model is never offered it"
    if e.get("clash"):
        return str(e["clash"])
    if e.get("requires_desk") in (True, "shell"):
        from . import desk
        if not desk.offered():
            return "needs a computer connected for computer use"
        if e["requires_desk"] == "shell" and not desk.shell_offered():
            return "needs shell granted for the connected computer (Settings, Computer use)"
    if e.get("requires_browser") is True:
        from . import browser
        if not browser.offered():
            return "needs the browser extension connected"
    if e.get("requires_local") is True:
        return "only offered in local chats"
    required = e.get("requires_settings")
    if isinstance(required, str):
        required = [required]
    missing = [str(k) for k in (required or []) if not getattr(settings, str(k), None)]
    if missing:
        return f"needs the setting {', '.join(missing)}"
    return ""


@router.get("/tools")
async def list_tools():
    """Everything in the registry, granted or not — the Tools tab reads this.
    `group` is yours (skills) / imported (OpenClaw, pinned) / builtin (tool
    folders shipped with the code). A built-in row also says how the model
    sees it (toolcatalog: `section`, `action`, `core`, `merged_into`,
    `internal`, `gating`); built-in rows come first-seen in the model's own
    order, and `sections` describes the sections in use, in that order."""
    entries = load_registry()
    cat = toolcatalog.catalogue(entries)
    out = []
    for e in entries:
        row = {
            "name": e["name"],
            "group": _group(e),
            "description": e.get("description", ""),
            "when_to_use": e.get("when_to_use", ""),
            "enabled": e.get("enabled", True) is not False and not e.get("clash"),
            "clash": e.get("clash", ""),
        }
        if row["group"] == "builtin":
            # `enabled` is the tool folder's own switch; `offered` is whether
            # the model is handed it right now, and `reason` is why not
            row["reason"] = _why_not(e)
            row["offered"] = not row["reason"]
            row.update(cat["rows"].get(e["name"]) or {
                "section": e.get("section") or "other", "action": None, "core": False,
                "merged_into": None, "internal": e.get("internal") is True, "gating": []})
        if row["group"] == "imported":
            row.update({
                "slug": e["dir"], "enabled": imported.offerable(e),
                "granted": e.get("granted", False), "blocked": e.get("blocked", ""),
                "requirements": e.get("requirements", []),
                "install_hints": e.get("install_hints", []),
                "pin": e.get("pin", {}), "homepage": e.get("homepage", ""),
                "body": e.get("body", ""),
            })
        out.append(row)
    rank = {n: i for i, n in enumerate(cat["order"])}
    # the built-in rows in the order the model's tool list has them, after the rest
    out.sort(key=lambda r: rank.get(r["name"], len(rank)) if r["group"] == "builtin" else -1)
    return {"tools": out, "sections": cat["sections"]}
