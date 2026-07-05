from backend.db import get_db, set_state
from backend.memory import read_project_md


async def run(slug: str) -> str:
    db = await get_db()
    try:
        async with db.execute(
            "SELECT slug, name FROM projects WHERE deleted_at IS NULL ORDER BY slug"
        ) as cur:
            rows = await cur.fetchall()
        valid = {r["slug"]: r["name"] for r in rows}
        if slug not in valid:
            options = ", ".join(valid) or "(none exist)"
            return f"error: no project '{slug}'. Available: {options}"
        await set_state(db, "active_project", slug)
        await db.commit()
    finally:
        await db.close()
    md = read_project_md(slug)
    return f"loaded project '{slug}'. Its project.md:\n\n{md[:4000]}"
