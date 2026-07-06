"""Durable memory: markdown files on the host + central-context assembly."""
import json
import re

import aiosqlite

from .config import settings, ensure_dirs
from .db import get_state


def estimate_tokens(text: str) -> int:
    """Cheap chars/4 estimate — for budgeting the context, not billing."""
    return max(0, round(len(text) / 4))


def _context_file(slug: str):
    return settings.projects_dir / slug / ".context.json"


def context_selection(slug: str) -> list[str]:
    p = _context_file(slug)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def set_context_selection(slug: str, files: list[str]) -> None:
    _context_file(slug).write_text(json.dumps(files))

SEEDS = {
    "soul.md": """# Soul — how Jarvis acts

You are Jarvis, the operator's personal assistant. You are concise, direct and
practical. No filler, no restating what the operator just said. When you don't
know something, say so. When a task is ambiguous, ask one sharp question rather
than guessing. You keep durable state in your memory files and project journals;
the sandbox VM you execute code in is disposable and holds nothing of value.

## Memory habit
Save things without being asked. Whenever the operator states a preference, a
fact about themselves or their setup, a decision, or corrects you — write it
down with memory_write before finishing your reply (short notes, stable names,
e.g. "operator-preferences"). Your context shows the list of notes you have;
when one looks relevant to the task at hand, read it with memory_read before
answering. After meaningful project work, update the journal.
""",
    "user.md": """# User

(Who the operator is and key info about them. Edit me.)
""",
    "env.md": """# Environment

(How to code and ship here, conventions, infrastructure notes. Edit me.)
""",
    "all-projects.md": """# All projects

(Thin summary of every project — always loaded into context. Regenerated automatically.)
""",
}

PROJECT_TEMPLATE = """# {name}

## Summary
{summary}

## Status
Just created.

## Issues
None yet.

## Journal
- {created}: project created.
"""


def ensure_memory_seeds() -> None:
    ensure_dirs()
    for fname, content in SEEDS.items():
        path = settings.memory_dir / fname
        if not path.exists():
            path.write_text(content)


def read_memory_file(name: str) -> str:
    path = settings.memory_dir / name
    return path.read_text() if path.exists() else ""


def write_memory_file(name: str, content: str) -> None:
    ensure_dirs()
    (settings.memory_dir / name).write_text(content)


def project_md_path(slug: str):
    return settings.projects_dir / slug / "project.md"


def read_project_md(slug: str) -> str:
    path = project_md_path(slug)
    return path.read_text() if path.exists() else ""


def extract_summary(project_md: str) -> str:
    """First paragraph of the '## Summary' section, for the thin all-projects rollup."""
    m = re.search(r"^## Summary\s*\n(.*?)(?=\n## |\Z)", project_md, re.M | re.S)
    if not m:
        return "(no summary)"
    text = m.group(1).strip()
    return text.split("\n\n")[0].strip() or "(no summary)"


async def refresh_all_projects(db: aiosqlite.Connection) -> None:
    async with db.execute(
        "SELECT slug, name FROM projects WHERE deleted_at IS NULL ORDER BY name"
    ) as cur:
        rows = await cur.fetchall()
    lines = ["# All projects", ""]
    if not rows:
        lines.append("(none yet)")
    for row in rows:
        summary = extract_summary(read_project_md(row["slug"]))
        lines.append(f"## {row['name']} (`{row['slug']}`)")
        lines.append(summary)
        lines.append("")
    write_memory_file("all-projects.md", "\n".join(lines).rstrip() + "\n")


async def get_active_project(db: aiosqlite.Connection) -> str | None:
    return await get_state(db, "active_project")


def agents_index() -> str:
    """Thin roster of defined agents so Jarvis knows what it can spawn_agent."""
    import yaml
    d = settings.agents_dir
    rosters = []
    if d.exists():
        for md in sorted(d.glob("*/AGENT.md")):
            if md.parent.name.startswith("."):
                continue
            try:
                text = md.read_text()
                fm = text.split("---")[1] if text.startswith("---") else ""
                meta = yaml.safe_load(fm) or {}
            except (IndexError, yaml.YAMLError, OSError):
                meta = {}
            desc = meta.get("description") or "(no description)"
            rosters.append(f"- {md.parent.name}: {desc}")
    if not rosters:
        return ""
    return ("# Agents you can summon with spawn_agent (by slug)\n"
            + "\n".join(rosters))


# How many tokens of memory notes to always carry in full. Notes are small;
# this comfortably fits preferences + bio + homelab. Anything past the budget
# is listed by name instead, recallable with memory_read.
MEMORY_CONTEXT_BUDGET = 2000


def _note_sort_key(path):
    # preferences first — the standing rules Jarvis must always honor
    name = path.stem.lower()
    return (0 if "pref" in name else 1, name)


def memory_block() -> str:
    """Full contents of the operator's memory notes, always in context so
    Jarvis honors standing facts and preferences without being reminded.
    Overflow past the budget degrades to a recall-by-name index."""
    notes = settings.memory_dir / "notes"
    files = sorted(notes.glob("*.md"), key=_note_sort_key) if notes.exists() else []
    if not files:
        return ""
    loaded, overflow, used = [], [], 0
    for p in files:
        try:
            text = p.read_text().strip()
        except OSError:
            continue
        toks = estimate_tokens(text)
        # always load at least the first (highest-priority) note in full
        if not loaded or used + toks <= MEMORY_CONTEXT_BUDGET:
            loaded.append(f"## {p.stem}\n{text}")
            used += toks
        else:
            overflow.append(p.stem)
    out = ["# Standing memory about the operator",
           "These are binding rules and preferences. Follow every one in EVERY "
           "response without being reminded. If a preference forbids something "
           "(e.g. a formatting habit), never do it.",
           *loaded]
    if overflow:
        out.append("Other notes (load with memory_read): " + ", ".join(overflow))
    return "\n\n".join(out)


def standing_rules_tail() -> str:
    """Restate the operator's hard preferences at the very END of the system
    prompt. Models weigh the start and end of context heavily and lose the
    middle ("lost in the middle"), so a single rule buried mid-prompt gets
    ignored. This compact imperative restatement is the bottom slice of the
    "task sandwich" — empirically it's what makes constraints actually stick on
    deepseek-v4-flash (0/5 em-dash violations with it, ~2/5 without)."""
    notes = settings.memory_dir / "notes"
    files = ([p for p in sorted(notes.glob("*.md"))
              if "pref" in p.stem.lower() or "rule" in p.stem.lower()]
             if notes.exists() else [])
    rules = []
    for p in files:
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for ln in lines:
            ln = ln.strip("-*# ").strip()
            if ln:
                rules.append(ln)
    if not rules:
        return ""
    out = ["# Operator rules (non-negotiable): apply to THIS reply",
           "Follow every rule below exactly. They override your persona and any "
           "stylistic habit."]
    out += [f"- {r}" for r in rules]
    return "\n".join(out)


_USE_DB = object()  # sentinel: "read the active project from the db"


async def assemble_system_prompt(db: aiosqlite.Connection, active=_USE_DB) -> str:
    """Central context: soul + user + env + thin all-projects (always) +
    agent roster + memory-notes index + the active project's full project.md
    (only when loaded). Pass `active=<slug>` to assemble for a specific project
    without touching global session state (scheduled/headless runs)."""
    ensure_memory_seeds()
    parts = [
        read_memory_file("soul.md"),
        # standing memory rides up top, right after the soul, so hard rules and
        # preferences get the model's attention instead of being buried deep
        memory_block(),
        "# About the user\n" + read_memory_file("user.md"),
        "# Environment\n" + read_memory_file("env.md"),
        read_memory_file("all-projects.md"),
        agents_index(),
    ]
    if active is _USE_DB:
        active = await get_active_project(db)
    if active:
        project_md = read_project_md(active)
        if project_md:
            parts.append(
                f"# Active project (loaded into central context): {active}\n\n{project_md}"
            )
        parts.extend(_loaded_context_files(active))
    # the sandwich bottom slice: hard rules restated LAST, after all context,
    # where they get the model's attention again
    parts.append(standing_rules_tail())
    return "\n\n---\n\n".join(p.strip() for p in parts if p.strip())


def _loaded_context_files(slug: str) -> list[str]:
    """Full contents of the files the operator ticked into context for this
    project. Missing/binary files are skipped silently (the picker guards them)."""
    out = []
    base = settings.projects_dir / slug
    for rel in context_selection(slug):
        path = base / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        out.append(f"# Loaded project file: {rel}\n\n```\n{text}\n```")
    return out
