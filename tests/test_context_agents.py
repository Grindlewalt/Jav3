"""Context-file loading + running an agent in the active project."""
import httpx
import pytest

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
        await c.post("/api/projects", json={"name": "Demo", "summary": "demo"})
        await c.post("/api/projects/demo/load")
        yield c


async def test_context_files_opt_in(client):
    await client.put("/api/projects/demo/file",
                     json={"path": "notes/spec.md", "content": "SECRET_MARKER content"})
    # nothing loaded by default
    r = await client.get("/api/projects/demo/context")
    body = r.json()
    assert body["selected_tokens"] == 0
    assert all(not f["selected"] for f in body["files"])
    spec = next(f for f in body["files"] if f["path"] == "notes/spec.md")
    assert spec["tokens"] > 0

    db = await get_db()
    try:
        assert "SECRET_MARKER" not in await assemble_system_prompt(db)
    finally:
        await db.close()

    # tick it -> now in context, tokens counted
    r = await client.put("/api/projects/demo/context",
                         json={"files": ["notes/spec.md"]})
    assert r.json()["files"] == ["notes/spec.md"]
    r = await client.get("/api/projects/demo/context")
    assert r.json()["selected_tokens"] > 0

    db = await get_db()
    try:
        assert "SECRET_MARKER" in await assemble_system_prompt(db)
    finally:
        await db.close()


async def test_context_rejects_unknown_file(client):
    r = await client.put("/api/projects/demo/context",
                         json={"files": ["does/not/exist.md"]})
    assert r.json()["files"] == []


async def test_context_only_when_active(client):
    await client.put("/api/projects/demo/file",
                     json={"path": "x.md", "content": "ONLYWHENACTIVE"})
    await client.put("/api/projects/demo/context", json={"files": ["x.md"]})
    await client.post("/api/projects/unload")
    db = await get_db()
    try:
        assert "ONLYWHENACTIVE" not in await assemble_system_prompt(db)
    finally:
        await db.close()


async def test_agent_run_creates_findable_conversation(client):
    await client.post("/api/agents", json={"name": "Scout"})
    # peak gate may or may not be active; pass confirm to be safe. The model
    # call will fail without a key, but the conversation + streaming wiring
    # is what we assert, and errors come back as SSE, not a 500.
    async with client.stream("POST", "/api/agents/scout/run",
                             json={"task": "look around", "confirm_peak": True}) as r:
        assert r.status_code == 200
        body = ""
        async for chunk in r.aiter_text():
            body += chunk
    assert '"type": "start"' in body
    assert '"agent": "Scout"' in body
    # agent runs are jobs now: absent from the chat sidebar, listed on /api/jobs
    r = await client.get("/api/conversations?project=demo")
    assert not any(c["summary"].startswith("[Scout]")
                   for c in r.json()["conversations"])
    jobs = (await client.get("/api/jobs")).json()["jobs"]
    assert any(j["summary"].startswith("[Scout]") and j["kind"] == "agent"
               and j["project"] == "demo" for j in jobs)


# --- labeled context blocks + agent context_exclude ---------------------------

async def test_exclude_drops_labeled_block(client):
    db = await get_db()
    try:
        base = await assemble_system_prompt(db)
        assert "# About the user" in base
        out = await assemble_system_prompt(db, exclude={"user.md"})
        assert "# About the user" not in out
        assert "# Environment" in out  # only the excluded block dropped
    finally:
        await db.close()


async def test_exclude_active_project_drops_project_and_context_files(client):
    await client.put("/api/projects/demo/file",
                     json={"path": "notes/spec.md", "content": "CTXFILE_MARKER"})
    await client.put("/api/projects/demo/context", json={"files": ["notes/spec.md"]})
    db = await get_db()
    try:
        base = await assemble_system_prompt(db)
        assert "# Active project" in base and "CTXFILE_MARKER" in base
        out = await assemble_system_prompt(db, exclude={"active-project"})
        assert "# Active project" not in out
        assert "CTXFILE_MARKER" not in out
    finally:
        await db.close()


async def test_operator_rules_never_excludable(client):
    notes = settings.memory_dir / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "operator-preferences.md").write_text(
        "- Never use semicolons in prose\n")
    db = await get_db()
    try:
        out = await assemble_system_prompt(db, exclude={"operator-rules"})
        assert "# Operator rules" in out  # the tail survives even if listed
    finally:
        await db.close()


async def test_empty_exclude_is_noop(client):
    db = await get_db()
    try:
        assert (await assemble_system_prompt(db)
                == await assemble_system_prompt(db, exclude=set()))
    finally:
        await db.close()
