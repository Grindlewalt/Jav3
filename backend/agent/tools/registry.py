"""Tool/skill registry: markdown + YAML frontmatter compiled to registry.json.

A tool is a FOLDER: tools/<name>/TOOL.md + tools/<name>/handler.py. That is
the entire contract for adding one — drop the folder in, it appears in the
Tools tab, flip `enabled: true` to grant it. (This is also the seam through
which Jarvis will one day author its own tools: two staged files + operator
approval.)

TOOL.md frontmatter:

    ---
    name: run_command
    description: Run a shell command in the sandbox VM.
    when_to_use: When code must be executed.
    enabled: false
    parameters:            # JSON schema for the arguments
      type: object
      properties: {...}
    ---
    (body = references / examples for the model)

handler.py must define `async def run(**args) -> str`. Legacy sources still
scanned: backend/agent/tools/defs/*.md (with @tool_handler registration) and
skills/<name>/SKILL.md. A registry entry without a handler is surfaced to the
model but fails loudly if called — that mismatch is a bug we want to see.
"""
import importlib.util
import json
import re
import traceback
from pathlib import Path
from typing import Awaitable, Callable

import yaml

from ...config import settings

TOOL_HANDLERS: dict[str, Callable[..., Awaitable[str]]] = {}

DEFS_DIR = Path(__file__).parent / "defs"

# handler.py modules loaded from tool folders, keyed by name, with the file
# mtime so an edited handler reloads without a restart.
_DYNAMIC: dict[str, tuple[float, Callable[..., Awaitable[str]]]] = {}


def tool_handler(name: str):
    def decorator(fn):
        TOOL_HANDLERS[name] = fn
        return fn
    return decorator


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
    return meta


def compile_registry() -> list[dict]:
    """Scan tool defs + skills, write data/registry.json, return the entries."""
    entries: list[dict] = []
    candidates: list[Path] = []
    if settings.tools_dir.exists():
        candidates += sorted(settings.tools_dir.glob("*/TOOL.md"))
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
        if e.get("body"):
            desc += f"\nNotes: {e['body'][:500]}"
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
    handler = TOOL_HANDLERS.get(name) or _load_dynamic(name)
    if handler is None:
        return f"error: tool '{name}' is registered but has no handler"
    try:
        return await handler(**args)
    except TypeError as e:
        return f"error: bad arguments for '{name}': {e}"
    except Exception:
        # The loop must observe failures, not die on them.
        return f"error: tool '{name}' raised:\n{traceback.format_exc(limit=4)}"
