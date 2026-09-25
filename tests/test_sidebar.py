"""GET /api/sidebar — the shell sidebar's one round-trip.

It must carry the same chat rows /api/conversations does (so the grouping
component can place them), the folders and visible projects, the live turns,
and `needs`: every scope with a pending git request, egress host or
operator-blocked plan item, attributed to that scope's most recent chat.
"""
import httpx
import pytest

from backend import chat as chat_mod
from backend.auth import hash_password
from backend.db import get_db, init_db, open_conversation
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


async def _chat(title: str, kind: str = "chat") -> int:
    db = await get_db()
    try:
        return await open_conversation(db, project=None, title=title, kind=kind)
    finally:
        await db.close()


async def _sql(q: str, args: tuple = ()) -> None:
    db = await get_db()
    try:
        await db.execute(q, args)
        await db.commit()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sidebar_shape(client):
    await client.post("/api/projects", json={"name": "Alpha"})
    f = (await client.post("/api/chat/folders", json={"name": "F"})).json()["folder"]
    a, b = await _chat("one"), await _chat("two")
    await _chat("job node", kind="subagent")
    await client.patch(f"/api/conversations/{a}", json={"folder_id": f["id"]})
    await client.patch(f"/api/conversations/{b}", json={"starred": True})

    r = await client.get("/api/sidebar")
    assert r.status_code == 200, r.text
    s = r.json()
    assert {c["id"] for c in s["conversations"]} == {a, b}, "job nodes stay out"
    rows = {c["id"]: c for c in s["conversations"]}
    assert rows[a]["folder_id"] == f["id"] and rows[b]["starred"] is True
    assert all("last_at" in c for c in s["conversations"])
    assert [x["name"] for x in s["folders"]] == ["F"] and s["folders"][0]["count"] == 1
    assert [p["slug"] for p in s["projects"]] == ["alpha"]
    assert s["running"] == [] and s["needs"] == []


@pytest.mark.asyncio
async def test_running_lists_live_turns(client):
    a, b = await _chat("idle"), await _chat("busy")
    chat_mod._active_turns[b] = object()
    try:
        s = (await client.get("/api/sidebar")).json()
    finally:
        chat_mod._active_turns.pop(b, None)
    assert [c["id"] for c in s["running"]] == [b]
    assert {c["id"]: c["running"] for c in s["conversations"]} == {a: False, b: True}


@pytest.mark.asyncio
async def test_needs_attributes_approvals_to_newest_chat_in_scope(client):
    await client.post("/api/projects", json={"name": "Alpha"})
    old, new = await _chat("old"), await _chat("new")
    for cid in (old, new):
        r = await client.patch(f"/api/conversations/{cid}",
                               json={"project": "alpha", "mode": "pin"})
        assert r.status_code == 200, r.text
    # the older chat spoke last, so it is the one that raised the request
    await _sql("INSERT INTO messages (conversation_id, role, content, created_at) "
               "VALUES (?, 'user', 'hi', datetime('now', '+1 minute'))", (old,))
    await _sql("INSERT INTO git_requests (project_slug, message) VALUES ('alpha', 'm')")
    await _sql("INSERT INTO egress_pending (project_slug, host) VALUES ('alpha', 'a.example')")
    await _sql("INSERT INTO egress_pending (project_slug, host) VALUES ('alpha', 'b.example')")
    await _sql("INSERT INTO egress_pending (project_slug, host, status) "
               "VALUES ('alpha', 'c.example', 'approved')")

    needs = (await client.get("/api/sidebar")).json()["needs"]
    assert len(needs) == 1
    n = needs[0]
    assert n["conversation_id"] == old and n["project"] == "alpha"
    assert (n["git"], n["egress"], n["plan"]) == (1, 2, 0)


@pytest.mark.asyncio
async def test_needs_for_a_projectless_chat_and_a_chatless_project(client):
    await client.post("/api/projects", json={"name": "Quiet"})
    cid = await _chat("loose")
    # a project-less chat's approvals live under its hidden chat-<id> store
    await _sql("INSERT INTO egress_pending (project_slug, host) VALUES (?, 'x.example')",
               (f"chat-{cid}",))
    # a plan item the operator must answer, on a project nobody is chatting in
    r = await client.post("/api/projects/quiet/plan/items",
                          json={"title": "decide the thing", "status": "blocked"})
    assert r.status_code == 200, r.text
    # an approval for a scope that is neither — shown on Security, not here
    await _sql("INSERT INTO git_requests (project_slug, message) VALUES ('gone', 'm')")
    # ...even when a chat is still pinned to that deleted project
    await client.post("/api/projects", json={"name": "Doomed"})
    pinned = await _chat("pinned to doomed")
    await client.patch(f"/api/conversations/{pinned}", json={"project": "doomed", "mode": "pin"})
    await _sql("INSERT INTO git_requests (project_slug, message) VALUES ('doomed', 'm')")
    await _sql("UPDATE projects SET deleted_at = datetime('now') WHERE slug = 'doomed'")

    needs = {n["scope"]: n for n in (await client.get("/api/sidebar")).json()["needs"]}
    assert set(needs) == {f"chat-{cid}", "quiet"}
    assert needs[f"chat-{cid}"]["conversation_id"] == cid
    assert needs[f"chat-{cid}"]["project"] is None
    assert needs["quiet"]["conversation_id"] is None and needs["quiet"]["plan"] == 1


@pytest.mark.asyncio
async def test_sidebar_requires_login(tmp_env):
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/sidebar")).status_code == 401
