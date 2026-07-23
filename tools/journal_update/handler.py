from datetime import date

from backend import writes
from backend.db import get_db
from backend.memory import read_project_md, refresh_all_projects
from backend.agent.tools.toolctx import require_project


async def run(entry: str) -> str:
    slug = await require_project()
    md = read_project_md(slug)
    line = f"- {date.today().isoformat()}: {entry.strip()}"
    if "## Journal" in md:
        md = md.rstrip() + "\n" + line + "\n"
    else:
        md = md.rstrip() + "\n\n## Journal\n" + line + "\n"
    # project.md is re-injected into every future system prompt — it MUST cross
    # the apply_write chokepoint (secret refusal + advisory scan), not write_text
    try:
        await writes.apply_write(slug, "project.md", md.encode())
    except writes.SecretLeakError as e:
        return f"error: journal update refused — {e}"
    db = await get_db()
    try:
        await refresh_all_projects(db)
    finally:
        await db.close()
    return "journal updated"
