"""Artifacts: project-less chats write into a hidden per-chat project that
auto-approves, stays out of every listing, and can be converted or merged."""
import httpx
import pytest

from backend import runtime
from backend.agent.tools import registry, toolctx
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import assemble_system_prompt, ensure_memory_seeds


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
        yield c


@pytest.fixture
async def artifact_store(client):
    """Simulate a project-less chat turn writing a file: set the contextvar
    the chat layer sets, then run the write_file tool."""
    token = runtime.artifact_slug.set("chat-77")
    out = await registry.dispatch(
        "write_file", {"path": "notes/plan.md", "content": "# The plan\nstep 1"})
    runtime.artifact_slug.reset(token)
    assert "staged write" in out
    return "chat-77"


async def test_write_lands_canonical_without_approval(client, artifact_store):
    p = settings.projects_dir / artifact_store / "notes/plan.md"
    assert p.read_text() == "# The plan\nstep 1\n" or p.read_text() == "# The plan\nstep 1"
    # nothing left waiting in the approval queue
    from backend.staging import list_staged
    assert list_staged(artifact_store) == []


async def test_no_fallback_without_contextvar(client):
    out = await registry.dispatch("write_file", {"path": "x.md", "content": "y"})
    assert out.startswith("error:") and "load_project" in out


async def test_hidden_everywhere(client, artifact_store):
    r = await client.get("/api/projects")
    assert all(p["slug"] != artifact_store for p in r.json()["projects"])
    db = await get_db()
    try:
        from backend.memory import refresh_all_projects
        await refresh_all_projects(db)
        prompt = await assemble_system_prompt(db, active=None)
    finally:
        await db.close()
    assert artifact_store not in prompt
    out = await registry.dispatch("load_project", {"slug": artifact_store})
    assert out.startswith("error:")          # not loadable as a normal project


async def test_artifacts_list_and_search(client, artifact_store):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO conversations (id, summary) VALUES (77, 'Budget planning')")
        await db.commit()
    finally:
        await db.close()
    r = await client.get("/api/artifacts")
    a = r.json()["artifacts"][0]
    assert a["slug"] == artifact_store and a["title"] == "Budget planning"
    assert [f["path"] for f in a["files"]] == ["notes/plan.md"]
    # content search hits, garbage misses
    assert (await client.get("/api/artifacts?q=step 1")).json()["artifacts"]
    assert not (await client.get("/api/artifacts?q=zzz-nope")).json()["artifacts"]


async def test_convert_to_project(client, artifact_store):
    r = await client.post(f"/api/artifacts/{artifact_store}/convert",
                          json={"name": "Budget"})
    assert r.status_code == 200
    r = await client.get("/api/projects")
    assert any(p["slug"] == artifact_store and p["name"] == "Budget"
               for p in r.json()["projects"])
    # marker gone -> future writes stage for approval again
    from backend.staging import is_artifact
    assert not is_artifact(artifact_store)


async def test_merge_stages_into_target(client, artifact_store):
    await client.post("/api/projects", json={"name": "Real"})
    r = await client.post(f"/api/artifacts/{artifact_store}/merge",
                          json={"target": "real"})
    assert r.json()["staged"] == ["notes/plan.md"]
    from backend.staging import list_staged
    assert [e["path"] for e in list_staged("real")] == ["notes/plan.md"]
    # canonical target file untouched until the operator approves
    assert not (settings.projects_dir / "real" / "notes/plan.md").exists()


async def test_ensure_artifact_project_idempotent(client):
    await toolctx._ensure_artifact_project("chat-88")
    await toolctx._ensure_artifact_project("chat-88")
    db = await get_db()
    try:
        async with db.execute(
            "SELECT COUNT(*) AS c FROM projects WHERE slug = 'chat-88'") as cur:
            assert (await cur.fetchone())["c"] == 1
    finally:
        await db.close()
