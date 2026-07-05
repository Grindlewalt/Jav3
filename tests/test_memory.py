from backend.config import settings
from backend.db import get_db, init_db, set_state
from backend.memory import (
    assemble_system_prompt,
    ensure_memory_seeds,
    extract_summary,
    project_md_path,
    refresh_all_projects,
)


def test_extract_summary():
    md = "# P\n\n## Summary\nBuilds a thing.\n\nMore detail here.\n\n## Status\nfine\n"
    assert extract_summary(md) == "Builds a thing."
    assert extract_summary("# P\n\n## Status\nx") == "(no summary)"


async def test_assemble_context_without_project(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        prompt = await assemble_system_prompt(db)
    finally:
        await db.close()
    assert "Jarvis" in prompt          # soul.md
    assert "# All projects" in prompt  # thin rollup always present
    assert "Active project" not in prompt


async def test_assemble_context_with_loaded_project(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO projects (slug, name, path) VALUES ('demo', 'Demo', '/tmp/demo')")
        await db.commit()
        md_path = project_md_path("demo")
        md_path.parent.mkdir(parents=True)
        md_path.write_text("# Demo\n\n## Summary\nA demo project.\n\n## Issues\nnone\n")
        await refresh_all_projects(db)
        await set_state(db, "active_project", "demo")
        prompt = await assemble_system_prompt(db)
    finally:
        await db.close()
    assert "Active project (loaded into central context): demo" in prompt
    assert "A demo project." in prompt
    # thin rollup picked up the summary too
    assert "## Demo (`demo`)" in prompt


async def test_notes_index_in_context(tmp_env):
    ensure_memory_seeds()
    notes = settings.memory_dir / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "operator-preferences.md").write_text("likes short messages\n")
    await init_db()
    db = await get_db()
    try:
        prompt = await assemble_system_prompt(db)
    finally:
        await db.close()
    assert "operator-preferences" in prompt
    assert "likes short messages" in prompt
    assert "Memory habit" in prompt
