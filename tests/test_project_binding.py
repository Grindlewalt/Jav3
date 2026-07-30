"""Per-project binding: a conversation/agent run is pinned to ITS project, not
the global active one — the seam that lets several chats/agents work different
projects at the same time."""
import httpx
import pytest

from backend import runtime
from backend.agent.tools import toolctx
from backend.auth import hash_password
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
        await c.post("/api/projects", json={"name": "Alpha", "summary": "a"})
        await c.post("/api/projects", json={"name": "Beta", "summary": "b"})
        await c.post("/api/projects/alpha/load")   # global active = alpha
        yield c


async def test_pinned_contextvar_beats_global_active(client):
    # global active is alpha, but a turn pinned to beta resolves beta
    assert await toolctx.active_slug() == "alpha"
    tok = runtime.active_project.set("beta")
    try:
        assert await toolctx.active_slug() == "beta"
    finally:
        runtime.active_project.reset(tok)
    # a pin of None (project-less operation) is honoured, not overridden
    tok = runtime.active_project.set(None)
    try:
        assert await toolctx.active_slug() is None
    finally:
        runtime.active_project.reset(tok)
    assert await toolctx.active_slug() == "alpha"


async def test_chat_rejects_unknown_project(client):
    # confirm_peak, or this asserts the wrong thing for seven hours a day: the
    # peak-pricing gate runs before the project lookup, so inside a peak window
    # an unknown project comes back 409 rather than 404. The ordering is
    # deliberate (it stops an orphan conversation being created), so the test
    # steps past the gate instead.
    r = await client.post("/api/chat", json={
        "message": "hi", "project": "nope", "confirm_peak": True})
    assert r.status_code == 404, r.text


async def test_agent_run_rejects_unknown_project(client):
    await client.post("/api/agents", json={"name": "Helper"})
    r = await client.post("/api/agents/helper/run",
                          json={"task": "x", "project": "nope"})
    assert r.status_code == 404


async def test_headless_run_inherits_parent_pin(client):
    """spawn_agent children resolve the parent turn's pinned project via the
    contextvar, not the global active project."""
    from backend.agents_api import _read
    from backend.agents_run import _USE_DB, _open_run
    await client.post("/api/agents", json={"name": "Helper"})
    tok = runtime.active_project.set("beta")
    db = await get_db()
    try:
        cid, active = await _open_run(db, _read("helper"), "do a thing",
                                      active=_USE_DB)
        assert active == "beta"                    # inherited pin, not alpha
        async with db.execute(
            "SELECT p.slug AS slug FROM conversations c JOIN projects p ON "
            "p.id = c.project_id WHERE c.id = ?", (cid,)) as cur:
            row = await cur.fetchone()
        assert row["slug"] == "beta"
    finally:
        await db.close()
        runtime.active_project.reset(tok)
