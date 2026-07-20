"""F5 interim: a chat turn that mutated the active project and didn't journal
gets one auto-written journal line; read-only or already-journaled turns don't."""
import pytest

from backend import chat as chat_mod
from backend.config import settings
from backend.db import get_db, init_db, set_state
from backend.memory import ensure_memory_seeds, read_project_md


@pytest.fixture
async def project_db(tmp_env, monkeypatch):
    await init_db()
    ensure_memory_seeds()
    proj = settings.projects_dir / "demo"
    proj.mkdir(parents=True)
    (proj / "project.md").write_text("# Demo\n\n## Summary\nx\n\n## Journal\n")
    db = await get_db()
    await set_state(db, "active_project", "demo")

    async def fake_line(system, user, temperature=0.3):
        return "Fixed the frobnicator and staged two files."
    from backend import summarize
    monkeypatch.setattr(summarize, "complete_text", fake_line)
    yield db
    await db.close()


async def _turn_with_tools(db, tools: list[str]) -> int:
    cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
    cid = cur.lastrowid
    for t in tools:
        await db.execute(
            "INSERT INTO tool_calls (conversation_id, tool, args) VALUES (?, ?, '{}')",
            (cid, t))
    await db.commit()
    return cid


async def test_auto_journal_after_project_writes(project_db):
    cid = await _turn_with_tools(project_db, ["read_file", "edit_file"])
    await chat_mod._auto_journal(project_db, cid, "fix the bug", "done", 0, "demo")
    md = read_project_md("demo")
    assert "(auto) Fixed the frobnicator" in md


async def test_no_journal_for_read_only_turns(project_db):
    cid = await _turn_with_tools(project_db, ["read_file", "web_search"])
    await chat_mod._auto_journal(project_db, cid, "look around", "done", 0, "demo")
    assert "(auto)" not in read_project_md("demo")


async def test_no_double_journal_when_model_journaled(project_db):
    cid = await _turn_with_tools(project_db, ["edit_file", "journal_update"])
    await chat_mod._auto_journal(project_db, cid, "fix", "done", 0, "demo")
    assert "(auto)" not in read_project_md("demo")


async def test_only_this_turns_tools_count(project_db):
    # the edit happened BEFORE this turn's snapshot — no auto entry
    cid = await _turn_with_tools(project_db, ["edit_file"])
    async with project_db.execute(
        "SELECT MAX(id) AS m FROM tool_calls WHERE conversation_id = ?",
        (cid,)) as cur:
        before = (await cur.fetchone())["m"]
    await chat_mod._auto_journal(project_db, cid, "chat", "done", before, "demo")
    assert "(auto)" not in read_project_md("demo")


async def test_kill_switch(project_db, monkeypatch):
    monkeypatch.setattr(settings, "auto_journal", False)
    cid = await _turn_with_tools(project_db, ["edit_file"])
    await chat_mod._auto_journal(project_db, cid, "fix", "done", 0, "demo")
    assert "(auto)" not in read_project_md("demo")
