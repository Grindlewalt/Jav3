"""operator_ask: an ask names the agent that asks, and stopping a project (or
everything) clears the asks of the agents beneath it (TUI-01, TUI-09)."""
import asyncio

import httpx
import pytest

from backend import operator_ask, runtime
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app

Q = [{"question": "Write a.txt?", "options": ["Yes", "No"]}]


@pytest.fixture
async def client(tmp_env):
    await init_db()
    operator_ask.reset_for_tests()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        yield c
    operator_ask.reset_for_tests()


async def _tree():
    """A project with an orchestrator chat, an agent under it, and one under that."""
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO projects (slug, name, path) VALUES ('p1', 'P1', 'x')")
        pid = cur.lastrowid
        cur = await db.execute("INSERT INTO conversations (summary, project_id) "
                               "VALUES ('orch', ?)", (pid,))
        root = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO conversations (summary, kind, parent_conversation_id, project_id, "
            "title) VALUES ('[item i1] Write c.txt', 'subagent', ?, ?, 'Write c.txt')",
            (root, pid))
        child = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO conversations (summary, kind, parent_conversation_id, project_id) "
            "VALUES ('grandchild worker', 'subagent', ?, ?)", (child, pid))
        grand = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    return root, child, grand


async def _ask_as(cid):
    t1 = runtime.conversation_id.set(cid)
    t2 = runtime.event_chan.set(f"node:{cid}")
    try:
        task = asyncio.create_task(operator_ask.ask(operator_ask.clean_questions(Q)))
        for _ in range(300):
            if any(a.conversation_id == cid for a in operator_ask._pending.values()):
                break
            await asyncio.sleep(0.01)
    finally:
        runtime.conversation_id.reset(t1)
        runtime.event_chan.reset(t2)
    return task


async def test_the_ask_event_names_the_agent(client):
    root, child, grand = await _tree()
    task = await _ask_as(child)
    ev = next(iter(operator_ask.pending_events(root)))
    assert ev["agent"] == "Write c.txt" and ev["conversation_id"] == child
    task.cancel()
    # no title: the task text, cleaned
    task = await _ask_as(grand)
    ev = next(e for e in operator_ask.pending_events(root) if e["conversation_id"] == grand)
    assert ev["agent"] == "grandchild worker"
    task.cancel()


async def test_stop_project_clears_the_asks_of_agents_beneath_it(client):
    root, child, grand = await _tree()
    t_child = await _ask_as(child)
    t_grand = await _ask_as(grand)
    assert len(operator_ask.pending_list()) == 2
    # nothing of the project is running as a turn or a run of its own: the agents
    # are guest nodes, and only their asks are alive
    r = await client.post("/api/chat/stop-project", json={"project": "p1"})
    assert r.status_code == 200
    for t in (t_child, t_grand):
        with pytest.raises(operator_ask.AskCancelled):
            await asyncio.wait_for(t, 2)
    assert operator_ask.pending_list() == []


async def test_stop_project_leaves_other_projects_asks(client):
    root, child, grand = await _tree()
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('elsewhere')")
        other = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    t_other = await _ask_as(other)
    t_child = await _ask_as(child)
    await client.post("/api/chat/stop-project", json={"project": "p1"})
    with pytest.raises(operator_ask.AskCancelled):
        await asyncio.wait_for(t_child, 2)
    assert [a["conversation_id"] for a in operator_ask.pending_list()] == [other]
    t_other.cancel()


async def test_stop_all_clears_every_ask(client):
    root, child, grand = await _tree()
    t_child = await _ask_as(child)
    r = await client.post("/api/chat/stop-all")
    assert r.status_code == 200
    with pytest.raises(operator_ask.AskCancelled):
        await asyncio.wait_for(t_child, 2)
    assert operator_ask.pending_list() == []
