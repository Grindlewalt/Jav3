from datetime import date

from backend.db import get_db
from backend.memory import project_md_path, read_project_md, refresh_all_projects
from backend.agent.tools.toolctx import require_project


async def run(entry: str) -> str:
    slug = await require_project()
    md = read_project_md(slug)
    line = f"- {date.today().isoformat()}: {entry.strip()}"
    if "## Journal" in md:
        md = md.rstrip() + "\n" + line + "\n"
    else:
        md = md.rstrip() + "\n\n## Journal\n" + line + "\n"
    project_md_path(slug).write_text(md)
    db = await get_db()
    try:
        await refresh_all_projects(db)
    finally:
        await db.close()
    return "journal updated"
