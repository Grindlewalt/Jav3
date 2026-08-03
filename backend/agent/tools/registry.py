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
import inspect
import json
import re
import traceback
from pathlib import Path
from typing import Awaitable, Callable

import yaml

from ...config import settings

# How much of a tool's TOOL.md body ships in its spec. Bounds a runaway body
# while fitting the curated guidance the complex tools (spawn_agent, research,
# create_agent, ...) genuinely need — 300 silently truncated their most important
# lines. Authors still keep bodies tight and lead with what matters most.
SPEC_NOTES_MAX = 600

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
        try:
            meta = _parse_md(path)
        except Exception:  # noqa: BLE001 — one broken TOOL.md must not take down
            continue       # the whole registry (and with it every chat turn)
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
    srcs = _sources()
    if any(p.stat().st_mtime > cached for p in srcs):
        return compile_registry()
    entries = json.loads(path.read_text())
    # a deleted tool folder never bumps a surviving file's mtime, so the check
    # above misses it — compare the source sets or its ghost entry lives forever
    if {e.get("source") for e in entries} != {str(p) for p in srcs}:
        return compile_registry()
    return entries


def read_only_names(entries: list[dict] | None = None) -> frozenset[str]:
    """Tools that declared `read_only: true` in frontmatter — the loop may run
    a round of these concurrently. Absent flag = assumed to write (fail closed)."""
    entries = entries if entries is not None else load_registry()
    return frozenset(e["name"] for e in entries if e.get("read_only") is True)


def openai_tool_specs(entries: list[dict] | None = None,
                      notes_max: int | None = None) -> list[dict]:
    """Registry entries in the wire format Model.complete expects.
    Entries with `enabled: false` are catalogued but not granted to the model.

    `notes_max` overrides SPEC_NOTES_MAX for this call; 0 drops the Notes
    bodies entirely. The voice local tier uses that — the bodies are written
    for a frontier model doing multi-step work and cost it 6.2k chars per
    turn, which a 4B pays for twice (prefill latency, and attention spent on
    guidance it will not use)."""
    entries = entries if entries is not None else load_registry()
    notes_cap = SPEC_NOTES_MAX if notes_max is None else notes_max
    specs = []
    for e in entries:
        if e.get("enabled") is False:
            continue
        desc = e["description"]
        if e.get("when_to_use"):
            desc += f" Use when: {e['when_to_use']}"
        # every enabled tool's spec ships on every turn, so the body slice is a
        # per-turn tax across the whole registry — keep it tight, and put the most
        # important line FIRST (a truncated tail is guidance the model never sees).
        # The cap only bites the few complex tools with long bodies; simple tools
        # pay nothing. Skills ship NO body (progressive disclosure): the listing is
        # for discovery; invoking the skill returns the full SKILL.md.
        if e.get("kind") == "skill":
            desc += " (Invoking this skill loads its full instructions.)"
        elif e.get("body") and notes_cap:
            desc += f"\nNotes: {e['body'][:notes_cap]}"
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
    try:
        handler = _load_dynamic(name)
    except Exception as e:  # noqa: BLE001 — a handler that fails to import
        # (syntax error, broken top-level import) must not kill the turn
        return (f"error: tool '{name}' handler failed to load: "
                f"{type(e).__name__}: {e}. Use a different tool.")
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
        # bind first so ONLY argument mismatches read as "bad arguments" —
        # a TypeError raised inside the handler is a real fault, not the
        # model's, and must keep its traceback
        inspect.signature(handler).bind(**args)
    except TypeError as e:
        return (f"error: bad arguments for '{name}': {e}. Check the tool's "
                "parameter schema and retry with corrected arguments.")
    try:
        return await handler(**args)
    except Exception as e:
        # The loop must observe failures, not die on them — and the message
        # should read as the first half of the fix, not just the fault.
        return (f"error: {name} failed with {type(e).__name__}: {e}. Adjust "
                "the arguments or try a different approach.\n"
                f"{traceback.format_exc(limit=4)}")
