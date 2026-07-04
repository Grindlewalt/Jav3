"""Tool/skill registry: markdown + YAML frontmatter compiled to registry.json.

A tool is a .md file in backend/agent/tools/defs/ or a skills/<name>/SKILL.md,
with frontmatter:

    ---
    name: run_in_vm
    description: Run a command in the sandbox VM.
    when_to_use: When code must be executed.
    parameters:            # JSON schema for the arguments
      type: object
      properties: {...}
    ---
    (body = references / examples, injected at selection time for skills)

Python handlers register with @tool_handler("name"). A registry entry without
a handler is surfaced to the model but fails loudly if called — that mismatch
is a bug we want to see.
"""
import json
import re
from pathlib import Path
from typing import Awaitable, Callable

import yaml

from ...config import settings

TOOL_HANDLERS: dict[str, Callable[..., Awaitable[str]]] = {}

DEFS_DIR = Path(__file__).parent / "defs"


def tool_handler(name: str):
    def decorator(fn):
        TOOL_HANDLERS[name] = fn
        return fn
    return decorator


def _parse_md(path: Path) -> dict | None:
    text = path.read_text()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return None
    meta = yaml.safe_load(m.group(1)) or {}
    if "name" not in meta or "description" not in meta:
        return None
    meta["body"] = m.group(2).strip()
    meta["source"] = str(path)
    return meta


def compile_registry() -> list[dict]:
    """Scan tool defs + skills, write data/registry.json, return the entries."""
    entries: list[dict] = []
    candidates: list[Path] = []
    if DEFS_DIR.exists():
        candidates += sorted(DEFS_DIR.glob("*.md"))
    if settings.skills_dir.exists():
        candidates += sorted(settings.skills_dir.glob("*/SKILL.md"))
    for path in candidates:
        meta = _parse_md(path)
        if meta:
            entries.append(meta)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "registry.json").write_text(json.dumps(entries, indent=2))
    return entries


def load_registry() -> list[dict]:
    path = settings.data_dir / "registry.json"
    if not path.exists():
        return compile_registry()
    return json.loads(path.read_text())


def openai_tool_specs(entries: list[dict] | None = None) -> list[dict]:
    """Registry entries in the wire format Model.complete expects."""
    entries = entries if entries is not None else load_registry()
    specs = []
    for e in entries:
        desc = e["description"]
        if e.get("when_to_use"):
            desc += f" Use when: {e['when_to_use']}"
        specs.append({
            "type": "function",
            "function": {
                "name": e["name"],
                "description": desc,
                "parameters": e.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return specs


async def dispatch(name: str, args: dict) -> str:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"error: tool '{name}' is registered but has no handler"
    return await handler(**args)
