"""WEBA-06: a chat pinned to a trashed project says so (and offers restore)
instead of crashing its file tools and letting the model re-pin the chat to a
look-alike project."""
import pytest

from backend import runtime
from backend.agent.tools import registry, toolctx
from backend.db import get_db
from tests.test_tools import client  # noqa: F401


async def _pinned_chat(client, *, trash: bool) -> int:
    await client.post("/api/projects", json={"name": "Demo Two", "summary": "look-alike"})
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO conversations (project_id) "
            "SELECT id FROM projects WHERE slug = 'demo'")
        cid = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    if trash:
        await client.delete("/api/projects/demo")
    runtime.conversation_id.set(cid)
    return cid


async def _pin(cid):
    db = await get_db()
    try:
        async with db.execute(
                "SELECT p.slug FROM conversations c JOIN projects p ON p.id = c.project_id "
                "WHERE c.id = ?", (cid,)) as cur:
            r = await cur.fetchone()
        return r["slug"] if r else None
    finally:
        await db.close()


async def test_load_project_will_not_re_pin_a_chat_whose_project_is_in_the_trash(client):
    cid = await _pinned_chat(client, trash=True)
    out = await registry.dispatch("load_project", {"slug": "demo-two"})
    assert out.startswith("error:") and "'demo'" in out and "Recently deleted" in out
    assert "restore" in out.lower() and "another project on your own" in out
    assert await _pin(cid) == "demo", "the pin was not moved by the model"


async def test_load_project_names_a_trashed_target(client):
    await _pinned_chat(client, trash=False)
    await client.post("/api/projects", json={"name": "Old", "summary": "x"})
    await client.delete("/api/projects/old")
    out = await registry.dispatch("load_project", {"slug": "old"})
    assert "Recently deleted" in out and "restore" in out.lower()


async def test_a_live_pin_still_moves_with_load_project(client):
    cid = await _pinned_chat(client, trash=False)
    out = await registry.dispatch("load_project", {"slug": "demo-two"})
    assert out.startswith("loaded project 'demo-two'")
    assert await _pin(cid) == "demo-two"


async def test_the_host_file_tools_say_the_project_is_in_the_trash(client):
    await _pinned_chat(client, trash=True)
    runtime.active_project.set(None)         # chat.py's join drops a trashed pin
    with pytest.raises(toolctx.NoProjectError) as e:
        await toolctx.require_project()
    msg = str(e.value)
    assert "in the trash" in msg and "'demo'" in msg and "Recently deleted" in msg
    out = await registry.dispatch("read_file", {"path": "a.txt"})
    assert "in the trash" in out and "harness fault" not in out
