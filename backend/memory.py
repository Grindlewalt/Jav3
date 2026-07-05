"""Durable memory: markdown files on the host + central-context assembly."""
import re

import aiosqlite

from .config import settings, ensure_dirs
from .db import get_state

SEEDS = {
    "soul.md": """# Soul — how Jarvis acts

You are Jarvis, the operator's personal assistant. You are concise, direct and
practical. No filler, no restating what the operator just said. When you don't
know something, say so. When a task is ambiguous, ask one sharp question rather
than guessing. You keep durable state in your memory files and project journals;
the sandbox VM you (will) execute code in is disposable and holds nothing of value.
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


async def assemble_system_prompt(db: aiosqlite.Connection) -> str:
    """Central context: soul + user + env + thin all-projects (always) +
    the active project's full project.md (only when loaded)."""
    ensure_memory_seeds()
    parts = [
        read_memory_file("soul.md"),
        "# About the user\n" + read_memory_file("user.md"),
        "# Environment\n" + read_memory_file("env.md"),
        read_memory_file("all-projects.md"),
    ]
    active = await get_active_project(db)
    if active:
        project_md = read_project_md(active)
        if project_md:
            parts.append(
                f"# Active project (loaded into central context): {active}\n\n{project_md}"
            )
    return "\n\n---\n\n".join(p.strip() for p in parts if p.strip())
