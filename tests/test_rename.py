"""Renaming a chat and a project.

A chat's title comes from one LLM pass after the first exchange and is never
revisited, so a conversation that wandered keeps a name about its opening
message. A project's name is set at creation. Both needed a way to be changed.

The interesting decision is what a project rename does NOT touch: the slug.
"""
import httpx
import pytest

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
            ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


@pytest.mark.asyncio
async def test_a_chat_can_be_renamed(client):
    r = await client.post("/api/chat", json={
        "message": "hello", "confirm_peak": True})
    # the turn runs detached; the row exists either way
    convos = (await client.get("/api/conversations")).json()["conversations"]
    assert convos, "no conversation was created"
    cid = convos[0]["id"]

    r = await client.patch(f"/api/conversations/{cid}",
                           json={"title": "  Weekend  plans  "})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Weekend plans", "whitespace should collapse"

    again = (await client.get("/api/conversations")).json()["conversations"]
    assert next(c for c in again if c["id"] == cid)["summary"] == "Weekend plans"


@pytest.mark.asyncio
async def test_renaming_a_chat_leaves_its_project_binding_alone(client):
    """A rename must not silently unpin the chat: the PATCH endpoint also
    assigns projects, and a title-only body used to fall through to the
    project-assignment path and reset the binding to 'follow'."""
    await client.post("/api/projects", json={"name": "Rename Target"})
    r = await client.post("/api/chat", json={
        "message": "hi", "project": "rename-target", "confirm_peak": True})
    convos = (await client.get("/api/conversations")).json()["conversations"]
    cid = convos[0]["id"]
    before = convos[0]["project_slug"]
    assert before == "rename-target"

    await client.patch(f"/api/conversations/{cid}", json={"title": "New name"})
    after = (await client.get("/api/conversations")).json()["conversations"]
    row = next(c for c in after if c["id"] == cid)
    assert row["summary"] == "New name"
    assert row["project_slug"] == before, "the rename dropped the project"


@pytest.mark.asyncio
async def test_a_blank_chat_title_is_refused(client):
    convos = (await client.get("/api/conversations")).json()["conversations"]
    if not convos:
        await client.post("/api/chat", json={"message": "x", "confirm_peak": True})
        convos = (await client.get("/api/conversations")).json()["conversations"]
    cid = convos[0]["id"]
    for blank in ("", "   ", "\n\t"):
        r = await client.patch(f"/api/conversations/{cid}", json={"title": blank})
        assert r.status_code == 400, blank


@pytest.mark.asyncio
async def test_renaming_an_unknown_chat_is_a_404(client):
    r = await client.patch("/api/conversations/999999", json={"title": "x"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_a_project_can_be_renamed_without_moving_it(client):
    """The slug is the directory on disk, the key in every conversation's
    binding, and what secret grants and egress policy are keyed on. Renaming it
    would mean moving a git repo and rewriting rows in several tables to change
    a label, so only the display name changes."""
    made = (await client.post("/api/projects", json={"name": "Old Name"})).json()
    slug = made["slug"]

    r = await client.put(f"/api/projects/{slug}/name", json={"name": "New Name"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "slug": slug, "name": "New Name"}

    projects = (await client.get("/api/projects")).json()["projects"]
    row = next(p for p in projects if p["slug"] == slug)
    assert row["name"] == "New Name"
    assert row["slug"] == slug, "the slug must not move"


@pytest.mark.asyncio
async def test_a_blank_project_name_is_refused(client):
    made = (await client.post("/api/projects", json={"name": "Keeps Name"})).json()
    r = await client.put(f"/api/projects/{made['slug']}/name", json={"name": "  "})
    assert r.status_code == 400
    projects = (await client.get("/api/projects")).json()["projects"]
    assert next(p for p in projects
                if p["slug"] == made["slug"])["name"] == "Keeps Name"


@pytest.mark.asyncio
async def test_renaming_an_unknown_project_is_a_404(client):
    r = await client.put("/api/projects/no-such-project/name", json={"name": "x"})
    assert r.status_code == 404
