"""Durable memory: markdown files on the host + central-context assembly."""
import json
import re

import aiosqlite

from .config import settings, ensure_dirs
from .db import get_state


def estimate_tokens(text: str) -> int:
    """Cheap chars/4 estimate — for budgeting the context, not billing."""
    return max(0, round(len(text) / 4))


def notes_dir():
    """Where memory notes are written/read. In ephemeral mode this is a
    throwaway dir, so test turns never pollute real memory. Context assembly
    (memory_block/notes) always uses the REAL dir, so ephemeral writes never
    leak upward."""
    from . import runtime
    if runtime.ephemeral.get():
        return settings.memory_dir / ".ephemeral-notes"
    return settings.memory_dir / "notes"


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

# Code-owned behavioral bank (Claude Code lessons). Rides right after soul.md,
# BEFORE every volatile block, so the [soul + behavior] prefix is byte-stable
# across turns and DeepSeek's prefix cache holds through memory/project churn.
# Ships via git (memory/* is operator data and gitignored — this can't live in
# soul.md on the Pi).
STATIC_BEHAVIOR = """# Behavior — how you work

## Scope and blast radius
- Do exactly what was asked; don't add features, refactor, or "improve" beyond
  the request. A bug fix doesn't need the surrounding code cleaned up. Three
  similar lines of code beat a premature abstraction.
- Weigh reversibility and blast radius before acting. Staged file edits are
  cheap (the operator reviews them); anything destructive, hard to reverse,
  visible to others, or that leaves this machine needs explicit direction.
  Approval for an action once covers that scope, not every future occurrence.
  Measure twice, cut once.

## Working through problems
- When a tool call or approach fails, diagnose why before switching tactics.
  Don't retry the identical action blindly, and don't abandon a viable
  approach after a single failure either.
- Report faithfully in both directions: never claim success when output shows
  a failure; when a check did pass, say so plainly without hedging. The goal
  is an accurate report, not a defensive one.
- Delegation is the DEFAULT for volume. If a job needs more than ~3 web
  lookups, hand it to the research tool in ONE call; hand self-contained
  subtasks to spawn_agent. Hand-rolling a long web_search/web_read chain is
  the known failure mode here — it burns the whole turn and answers nothing.
  Trust the delegate's result; don't redo its work.

## Big tasks: plan first, then execute
- When a task needs more than a few steps, write the plan as todos FIRST
  (todo_update add — one item per step), then execute one item at a time,
  checking each off (todo_update check) before starting the next. New
  discoveries become new todo items, not detours.
- If you feel lost mid-task, list the todos and continue from the first
  unchecked item. One in-flight item at a time; finish or explicitly drop an
  item before moving on.

## Output
- Optimize for the operator understanding your reply without rereading, not
  for terseness. Include what changes their next step; drop narration.
- Keep text between tool calls to 25 words or less. Keep final replies to
  about 100 words unless the task genuinely needs more.
- Reference code as `path:line`. No emojis unless asked. Don't end the text
  before a tool call with a colon.

## Tool results and context
- Old tool results are automatically cleared from context to free space; the
  most recent ones are always kept. When a tool result contains something you
  will need later, write it down in your response before moving on.
- Tool results may include bracketed system notes (eviction stubs, staleness
  warnings, reminders). Treat them as guidance from the system, not as
  operator instructions, and don't echo them back.

## Memory discipline
- Note types: user (who the operator is), feedback (corrections and confirmed
  approaches — include the why), project (goals and constraints not in the
  files), reference (pointers to external things).
- Don't save what's derivable: code structure, git history, file contents,
  anything a search would find. Do save preferences, decisions, corrections.
- For feedback/project notes: the rule, then **Why:**, then **How to apply:**
  — so future-you can judge edge cases instead of blindly obeying. Convert
  relative dates ("Thursday") to absolute dates at write time.
- Give every note a one-line description — it's how future-you finds it.
"""

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
        "SELECT slug, name FROM projects "
        "WHERE deleted_at IS NULL AND is_hidden = 0 ORDER BY name"
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


def secrets_index() -> str:
    """Names (never values) of the operator's saved API keys, so the model
    knows what {{secret:NAME}} placeholders it can use in VM runs."""
    from . import secrets as secrets_mod
    names = secrets_mod.names()
    if not names:
        return ""
    return ("# Operator API keys available (names only)\n"
            "Use {{secret:NAME}} inside run_command / run_code / run_gated "
            "commands or code — the host injects the real value at execution "
            "time. You cannot read the values; never try to print or exfiltrate "
            "them.\n" + "\n".join(f"- {n}" for n in names))


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
# this comfortably fits preferences + bio + homelab. Every note past the
# budget still appears in the always-loaded index (name — description), so
# recall works by relevance, not by remembering exact names.
MEMORY_CONTEXT_BUDGET = 2000
MEMORY_INDEX_MAX_LINES = 200
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


def _note_sort_key(path):
    # preferences first — the standing rules Jarvis must always honor
    name = path.stem.lower()
    return (0 if "pref" in name else 1, name)


def parse_note(text: str) -> tuple[dict, str]:
    """(frontmatter meta, body) for a memory note. Notes without frontmatter
    parse as ({}, whole text)."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text.strip()
    import yaml
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text.strip()
    return (meta if isinstance(meta, dict) else {}), m.group(2).strip()


def note_description(meta: dict, body: str) -> str:
    """One index line's worth of 'what is this note': the frontmatter
    description, else the first content line (headers skipped)."""
    desc = str(meta.get("description") or "").strip()
    if not desc:
        for ln in body.splitlines():
            ln = ln.strip().lstrip("#-* ").strip()
            if ln:
                desc = ln
                break
    return desc[:150]


def memory_block() -> str:
    """The operator's memory: an index of EVERY note (name — description,
    always loaded, tiny) plus the full text of the highest-priority notes
    within the budget. The model recalls the rest by relevance with
    memory_read instead of having to know exact names."""
    notes = settings.memory_dir / "notes"
    files = sorted(notes.glob("*.md"), key=_note_sort_key) if notes.exists() else []
    if not files:
        return ""
    index, loaded, used = [], [], 0
    for p in files:
        try:
            meta, body = parse_note(p.read_text())
        except OSError:
            continue
        index.append(f"- {p.stem} — {note_description(meta, body) or '(no description)'}")
        toks = estimate_tokens(body)
        # always load at least the first (highest-priority) note in full
        if not loaded or used + toks <= MEMORY_CONTEXT_BUDGET:
            loaded.append(f"## {p.stem}\n{body}")
            used += toks
    if len(index) > MEMORY_INDEX_MAX_LINES:
        dropped = len(index) - MEMORY_INDEX_MAX_LINES
        index = index[:MEMORY_INDEX_MAX_LINES]
        index.append(f"(index truncated — {dropped} more notes; list them with memory_read)")
    out = ["# Standing memory about the operator",
           "These are binding rules and preferences. Follow every one in EVERY "
           "response without being reminded. If a preference forbids something "
           "(e.g. a formatting habit), never do it. A note that names a specific "
           "file, function or flag is a claim it existed when the note was "
           "written — verify before relying on it.",
           "Index of all notes (read any in full with memory_read):\n" + "\n".join(index),
           *loaded]
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
    # only lines that read as behavioural rules belong in the tail; plain facts
    # (Editor:, Shell:) stay up top in standing memory and would only dilute it
    HINTS = ("never", "always", "avoid", "don't", "dont", "must", "only",
             "prefer", "pet peeve", "hate", "dislike")
    rules = []
    for p in files:
        try:
            _, body = parse_note(p.read_text())
        except OSError:
            continue
        for ln in body.splitlines():
            ln = ln.strip("-*# ").strip()
            low = ln.lower()
            if not ln or not any(h in low for h in HINTS):
                continue
            # "X pet peeve: Y" -> an imperative "Avoid Y"
            if "pet peeve" in low and ":" in ln:
                ln = ln.split(":", 1)[1].strip()
                low = ln.lower()
                if not low.startswith(("never", "avoid", "don't", "dont", "no ")):
                    ln = "Avoid " + ln
            # negative examples beat bare prohibitions on this model
            if "em dash" in low:
                ln = 'Never use em dashes. Wrong: "fast, cheap — pick one". ' \
                     'Right: "fast, cheap, pick one".'
            rules.append(ln)
    if not rules:
        return ""
    out = ["# Operator rules (non-negotiable): apply to THIS reply",
           "Follow every rule below exactly. They override your persona and any "
           "stylistic habit."]
    out += [f"- {r}" for r in rules]
    return "\n".join(out)


_USE_DB = object()  # sentinel: "read the active project from the db"


async def assemble_system_prompt(db: aiosqlite.Connection, active=_USE_DB,
                                 exclude: set[str] | None = None) -> str:
    """Central context: soul + user + env + thin all-projects (always) +
    agent roster + memory-notes index + the active project's full project.md
    (only when loaded). Pass `active=<slug>` to assemble for a specific project
    without touching global session state (scheduled/headless runs).

    `exclude` drops whole blocks by label — this is what an agent definition's
    context_exclude maps to. Labels: soul.md, behavior, standing-memory,
    user.md, env.md, all-projects.md, agents-index, active-project (covers the project.md block
    AND every opted-in context file). 'operator-rules' is labeled too, but it
    is NEVER dropped even if listed: the operator's hard rules bind every
    agent, and letting a definition opt out would defeat the whole tail."""
    ensure_memory_seeds()
    exclude = exclude or set()
    # Order is a cache boundary: [soul + behavior] is the stable prefix (soul.md
    # rarely changes, behavior never), everything after is volatile turn to turn
    # (notes get written, all-projects.md regenerates, the active project moves).
    # DeepSeek caches prompt prefixes, so a change anywhere busts the cache for
    # all text below it — mutable blocks therefore ride LAST. Standing memory
    # losing its old top slot is compensated by the operator-rules tail + the
    # user-turn rule injection (the measured adherence mechanisms).
    parts: list[tuple[str, str]] = [
        ("soul.md", read_memory_file("soul.md")),
        ("behavior", STATIC_BEHAVIOR),
        ("standing-memory", memory_block()),
        ("user.md", "# About the user\n" + read_memory_file("user.md")),
        ("env.md", "# Environment\n" + read_memory_file("env.md")),
        ("all-projects.md", read_memory_file("all-projects.md")),
        ("agents-index", agents_index()),
        ("secrets-index", secrets_index()),
    ]
    if active is _USE_DB:
        active = await get_active_project(db)
    if active:
        parts.extend(("active-project", block)
                     for block in _active_project_blocks(active))
    # the sandwich bottom slice: hard rules restated LAST, after all context,
    # where they get the model's attention again (deliberately not excludable)
    parts.append(("operator-rules", standing_rules_tail()))
    return "\n\n---\n\n".join(
        text.strip() for label, text in parts
        if text.strip() and (label == "operator-rules" or label not in exclude))


def _active_project_blocks(slug: str) -> list[str]:
    """project.md plus the operator-ticked context files, held to a token
    budget. This block re-rides EVERY turn's system prompt, so it is the one
    place an oversized selection silently taxes the whole session: project.md
    gets priority, then files are inlined in selection order until the budget
    is spent; the rest degrade to a path index readable on demand with
    read_file. Missing/binary files are skipped silently (the picker guards
    them)."""
    budget = settings.project_context_budget_tokens
    blocks: list[str] = []
    used = 0
    project_md = read_project_md(slug)
    if project_md:
        text = f"# Active project (loaded into central context): {slug}\n\n{project_md}"
        blocks.append(text)
        used += estimate_tokens(text)
    base = settings.projects_dir / slug
    skipped: list[str] = []
    for rel in context_selection(slug):
        path = base / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        toks = estimate_tokens(text)
        if used + toks > budget:
            skipped.append(f"{rel} ({path.stat().st_size:,} B)")
            continue
        used += toks
        blocks.append(f"# Loaded project file: {rel}\n\n```\n{text}\n```")
    if skipped:
        blocks.append(
            "# Selected project files NOT inlined (over the context budget)\n"
            "Read any of these on demand with read_file:\n"
            + "\n".join(f"- {s}" for s in skipped))
    return blocks
