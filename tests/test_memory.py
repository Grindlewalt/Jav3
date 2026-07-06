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


async def test_memory_notes_fully_in_context(tmp_env):
    ensure_memory_seeds()
    notes = settings.memory_dir / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    # a multi-line note: the whole point is that lines beyond the first are in
    # context, so preferences like "never use em dashes" are always honored
    (notes / "operator-preferences.md").write_text(
        "Editor: helix\nShell: bash\nnever use em dashes\n")
    await init_db()
    db = await get_db()
    try:
        prompt = await assemble_system_prompt(db)
    finally:
        await db.close()
    assert "operator-preferences" in prompt
    assert "never use em dashes" in prompt   # not just the first line
    assert "Memory habit" in prompt


async def test_memory_overflow_degrades_to_index(tmp_env, monkeypatch):
    import backend.memory as m
    monkeypatch.setattr(m, "MEMORY_CONTEXT_BUDGET", 30)
    ensure_memory_seeds()
    notes = settings.memory_dir / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "operator-preferences.md").write_text("never use em dashes\n")
    (notes / "long-note.md").write_text("word " * 500)
    await init_db()
    db = await get_db()
    try:
        prompt = await assemble_system_prompt(db)
    finally:
        await db.close()
    assert "never use em dashes" in prompt          # priority note loaded in full
    assert "load with memory_read" in prompt         # overflow degraded to index
    assert "long-note" in prompt



async def test_standing_rules_restated_at_end(tmp_env):
    ensure_memory_seeds()
    notes = settings.memory_dir / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "operator-preferences.md").write_text(
        "Editor: helix\nnever use em dashes\nbe concise\n")
    await init_db()
    db = await get_db()
    try:
        prompt = await assemble_system_prompt(db)
    finally:
        await db.close()
    # the rule appears both up top (standing memory) and restated at the very end
    assert prompt.count("never use em dashes") >= 2
    tail = prompt.rsplit("---", 1)[-1]
    assert "Operator rules (non-negotiable)" in tail
    assert "never use em dashes" in tail
