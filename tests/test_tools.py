"""Tool layer: folder discovery, staged-write semantics, approval flow."""
import httpx
import pytest

from backend.agent.tools import registry
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds
from backend.staging import list_staged


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


async def test_write_is_staged_not_canonical(client):
    out = await registry.dispatch("write_file",
                                  {"path": "code/x.py", "content": "print(1)\n"})
    assert "staged" in out
    assert not (settings.projects_dir / "demo" / "code" / "x.py").exists()
    assert [e["path"] for e in list_staged("demo")] == ["code/x.py"]
    # the agent sees its own staged edit
    assert "print(1)" in await registry.dispatch("read_file", {"path": "code/x.py"})
    listing = await registry.dispatch("list_files", {})
    assert "code/x.py" in listing and "staged" in listing


async def test_edit_requires_unique_match(client):
    await registry.dispatch("write_file", {"path": "a.txt", "content": "x y x"})
    out = await registry.dispatch("edit_file",
                                  {"path": "a.txt", "find": "x", "replace": "z"})
    assert "matches 2" in out
    out = await registry.dispatch("edit_file",
                                  {"path": "a.txt", "find": "x", "replace": "z", "all": True})
    assert "staged" in out
    assert "z y z" in await registry.dispatch("read_file", {"path": "a.txt"})


async def test_approval_flow(client):
    await registry.dispatch("write_file", {"path": "b.txt", "content": "hello"})
    r = await client.get("/api/projects/demo/staging")
    assert [e["path"] for e in r.json()["staged"]] == ["b.txt"]
    r = await client.get("/api/projects/demo/staging/diff", params={"path": "b.txt"})
    assert r.json()["old"] is None and r.json()["new"] == "hello"
    r = await client.post("/api/projects/demo/staging/approve", json={"paths": ["b.txt"]})
    assert r.json()["applied"] == ["b.txt"]
    assert (settings.projects_dir / "demo" / "b.txt").read_text() == "hello"
    assert list_staged("demo") == []


async def test_reject_discards(client):
    await registry.dispatch("write_file", {"path": "c.txt", "content": "bad"})
    r = await client.post("/api/projects/demo/staging/reject", json={"paths": None})
    assert r.json()["rejected"] == ["c.txt"]
    assert list_staged("demo") == []
    assert not (settings.projects_dir / "demo" / "c.txt").exists()


async def test_staging_cannot_escape(client):
    out = await registry.dispatch("write_file",
                                  {"path": "../../evil.txt", "content": "x"})
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
