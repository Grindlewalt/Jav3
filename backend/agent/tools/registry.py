"""Tool/skill registry: markdown + YAML frontmatter compiled to registry.json.

A tool is a FOLDER: tools/<name>/TOOL.md + tools/<name>/handler.py. That is
the entire contract for adding one — drop the folder in, it appears in the
Tools tab, flip `enabled: true` to grant it. (This is also the seam through
which Jarvis will one day author its own tools: two staged files + operator
approval.)

TOOL.md frontmatter:

    ---
    name: web_search
    description: Search the web via SearXNG.
    when_to_use: When the answer needs live information.
    enabled: false
    parameters:            # JSON schema for the arguments
      type: object
      properties: {...}
    ---
    (body = references / examples for the model)

handler.py must define `async def run(**args) -> str`. Skills are compiled into
the same registry from skills/<name>/SKILL.md. A registry entry without a
handler is surfaced to the model but fails loudly if called — that mismatch is
a bug we want to see.
"""
import importlib.util
import json
import re
import traceback
from pathlib import Path
from typing import Awaitable, Callable

import yaml

from ...config import settings

# handler.py modules loaded from tool folders, keyed by name, with the file
# mtime so an edited handler reloads without a restart.
_DYNAMIC: dict[str, tuple[float, Callable[..., Awaitable[str]]]] = {}


def _load_dynamic(name: str) -> Callable[..., Awaitable[str]] | None:
    path = settings.tools_dir / name / "handler.py"
    if not path.is_file():
        return None
    mtime = path.stat().st_mtime
    cached = _DYNAMIC.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    spec = importlib.util.spec_from_file_location(f"jarvis_tool_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, "run", None)
    if fn is None:
        return None
    _DYNAMIC[name] = (mtime, fn)
    return fn


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
    meta["kind"] = "skill" if path.name == "SKILL.md" else "tool"
    return meta


def _sources() -> list[Path]:
    out: list[Path] = []
    if settings.tools_dir.exists():
        out += sorted(settings.tools_dir.glob("*/TOOL.md"))
    if settings.skills_dir.exists():
        out += sorted(settings.skills_dir.glob("*/SKILL.md"))
    return out


def compile_registry() -> list[dict]:
    """Scan tool defs + skills, write data/registry.json, return the entries."""
    entries: list[dict] = []
    for path in _sources():
        meta = _parse_md(path)
        if meta:
            entries.append(meta)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "registry.json").write_text(json.dumps(entries, indent=2))
    return entries


def load_registry() -> list[dict]:
    """Cached registry, recompiled whenever any TOOL.md/SKILL.md is newer than
    the cache — handlers already hot-reload by mtime, so the specs should too
    (a stale spec meant an edited TOOL.md wasn't seen until restart)."""
    path = settings.data_dir / "registry.json"
    if not path.exists():
        return compile_registry()
    cached = path.stat().st_mtime
    if any(p.stat().st_mtime > cached for p in _sources()):
        return compile_registry()
    return json.loads(path.read_text())


def read_only_names(entries: list[dict] | None = None) -> frozenset[str]:
    """Tools that declared `read_only: true` in frontmatter — the loop may run
    a round of these concurrently. Absent flag = assumed to write (fail closed)."""
    entries = entries if entries is not None else load_registry()
    return frozenset(e["name"] for e in entries if e.get("read_only") is True)


def openai_tool_specs(entries: list[dict] | None = None) -> list[dict]:
    """Registry entries in the wire format Model.complete expects.
    Entries with `enabled: false` are catalogued but not granted to the model."""
    entries = entries if entries is not None else load_registry()
    specs = []
    for e in entries:
        if e.get("enabled") is False:
            continue
        desc = e["description"]
        if e.get("when_to_use"):
            desc += f" Use when: {e['when_to_use']}"
        # every enabled tool's spec ships on every turn, so the body slice is a
        # per-turn tax across the whole registry — keep it tight. Skills ship
        # NO body at all (progressive disclosure): the listing is for
        # discovery; invoking the skill returns the full SKILL.md.
        if e.get("kind") == "skill":
            desc += " (Invoking this skill loads its full instructions.)"
        elif e.get("body"):
            desc += f"\nNotes: {e['body'][:300]}"
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
    handler = _load_dynamic(name)
    if handler is None:
        entry = next((e for e in load_registry() if e["name"] == name), None)
        if entry and entry.get("kind") == "skill":
            # a skill IS its instructions: invoking it injects the full
            # SKILL.md body the spec deliberately left out
            body = entry.get("body") or "(this skill has no instructions yet)"
            return (f"[skill {name} loaded — follow these instructions now, "
                    f"using the arguments you passed: {json.dumps(args)}]\n{body}")
        return f"error: tool '{name}' is registered but has no handler"
    try:
        return await handler(**args)
    except TypeError as e:
        return (f"error: bad arguments for '{name}': {e}. Check the tool's "
                "parameter schema and retry with corrected arguments.")
    except Exception as e:
        # The loop must observe failures, not die on them — and the message
        # should read as the first half of the fix, not just the fault.
        return (f"error: {name} failed with {type(e).__name__}: {e}. Adjust "
                "the arguments or try a different approach.\n"
                f"{traceback.format_exc(limit=4)}")
