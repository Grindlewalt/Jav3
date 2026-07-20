"""Tool layer: folder discovery, direct-write semantics, path safety."""
import httpx
import pytest

from backend.agent.tools import registry
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("operator", hash_password("hunter2")),
        )
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        await c.post("/api/projects", json={"name": "Demo", "summary": "demo"})
        await c.post("/api/projects/demo/load")
        yield c


def test_registry_discovers_folder_tools(tmp_env):
    names = {e["name"] for e in registry.compile_registry()}
    for expected in ("list_files", "read_file", "write_file", "edit_file",
                     "memory_write", "memory_read",
                     "journal_update", "todo_update"):
        assert expected in names
    granted = {s["function"]["name"] for s in registry.openai_tool_specs()}
    assert "write_file" in granted


async def test_write_lands_canonical_immediately(client):
    out = await registry.dispatch("write_file",
                                  {"path": "code/x.py", "content": "print(1)\n"})
    assert "wrote" in out
    p = settings.projects_dir / "demo" / "code" / "x.py"
    assert p.read_text() == "print(1)\n"
    assert not (p.stat().st_mode & 0o111)   # never executable
    assert "print(1)" in await registry.dispatch("read_file", {"path": "code/x.py"})
    listing = await registry.dispatch("list_files", {})
    assert "code/x.py" in listing


async def test_edit_requires_unique_match(client):
    await registry.dispatch("write_file", {"path": "a.txt", "content": "x y x"})
    out = await registry.dispatch("edit_file",
                                  {"path": "a.txt", "find": "x", "replace": "z"})
    assert "matches 2" in out
    out = await registry.dispatch("edit_file",
                                  {"path": "a.txt", "find": "x", "replace": "z", "all": True})
    assert "edited" in out
    assert (settings.projects_dir / "demo" / "a.txt").read_text() == "z y z"


async def test_staging_endpoints_are_gone(client):
    r = await client.get("/api/projects/demo/staging")
    assert r.status_code in (404, 405)
    r = await client.post("/api/projects/demo/staging/approve", json={"paths": None})
    assert r.status_code in (404, 405)


async def test_write_cannot_escape(client):
    out = await registry.dispatch("write_file",
                                  {"path": "../../evil.txt", "content": "x"})
    assert "error" in out.lower()
    assert not (settings.projects_dir.parent / "evil.txt").exists()


async def test_write_cannot_touch_git(client):
    out = await registry.dispatch("write_file",
                                  {"path": ".git/hooks/pre-commit", "content": "#!/bin/sh\n"})
    assert "error" in out.lower()


async def test_memory_and_todos(client):
    out = await registry.dispatch("memory_write",
                                  {"name": "Test Note!", "content": "remember me"})
    assert "test-note" in out
    assert "remember me" in await registry.dispatch("memory_read", {"name": "test-note"})
    out = await registry.dispatch("todo_update", {"action": "add", "text": "ship it"})
    assert "[ ] ship it" in out
    out = await registry.dispatch("todo_update", {"action": "check", "index": 0})
    assert "[x] ship it" in out


async def test_journal_update(client):
    await registry.dispatch("journal_update", {"entry": "tools came online"})
    md = (settings.projects_dir / "demo" / "project.md").read_text()
    assert "tools came online" in md


async def test_no_project_loaded(client):
    await client.post("/api/projects/unload")
    out = await registry.dispatch("list_files", {})
    assert "no project is loaded" in out


async def test_dispatch_survives_bad_args(client):
    out = await registry.dispatch("read_file", {"nope": 1})
    assert out.startswith("error:")


async def test_load_project_tool(client):
    out = await registry.dispatch("load_project", {"slug": "nope"})
    assert "error" in out and "demo" in out
    await client.post("/api/projects", json={"name": "Second", "summary": "two"})
    out = await registry.dispatch("load_project", {"slug": "second"})
    assert "loaded project 'second'" in out
    assert "no project is loaded" not in await registry.dispatch("list_files", {})
    r = await client.get("/api/projects")
    assert r.json()["active"] == "second"


async def test_load_project_in_conversation_rebinds_only_that_chat(client):
    """Inside a turn (conversation_id contextvar set), load_project pins the
    conversation instead of yanking the global session state."""
    from backend import runtime
    from backend.db import get_db, open_conversation
    db = await get_db()
    try:
        cid = await open_conversation(db, project="demo", title="t")
    finally:
        await db.close()
    await client.post("/api/projects", json={"name": "Third", "summary": "three"})
    tok = runtime.conversation_id.set(cid)
    try:
        out = await registry.dispatch("load_project", {"slug": "third"})
        assert "loaded project 'third'" in out
    finally:
        runtime.conversation_id.reset(tok)
    r = await client.get("/api/projects")
    assert r.json()["active"] == "demo"          # global untouched
    db = await get_db()
    try:
        async with db.execute(
            "SELECT p.slug AS slug FROM conversations c JOIN projects p "
            "ON p.id = c.project_id WHERE c.id = ?", (cid,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    assert row["slug"] == "third"                # the chat is rebound
