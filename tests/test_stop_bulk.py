"""POST /api/chat/stop-all and /api/chat/stop-project: bulk stop, with a dry
run that only counts, scoped by who asks (a device token: its own turns)."""
import asyncio

import httpx
import pytest

from backend import agents_run, chat as chat_mod, plan as plan_mod
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
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.execute("INSERT INTO projects (id, slug, name, path) VALUES "
                         "(1, 'alpha', 'Alpha', '/x'), (2, 'beta', 'Beta', '/y')")
        # 1: chat in alpha; 2: its agent child (no project of its own);
        # 3: chat in beta; 4: an agent run in alpha
        for cid, pid, parent in ((1, 1, None), (2, None, 1), (3, 2, None), (4, 1, None)):
            await db.execute("INSERT INTO conversations (id, project_id, "
                             "parent_conversation_id) VALUES (?, ?, ?)", (cid, pid, parent))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        yield c


@pytest.fixture
async def running(monkeypatch):
    def task():
        return asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    turns = {1: task(), 2: task(), 3: task()}
    runs = {4: task()}
    plans = {"alpha": task()}
    monkeypatch.setattr(chat_mod, "_active_turns", turns)
    monkeypatch.setattr(chat_mod, "_turn_actors", {1: "session", 2: "session",
                                                   3: "device:7"})
    monkeypatch.setattr(agents_run, "_active_runs", runs)
    monkeypatch.setattr(plan_mod, "_runs", plans)
    yield turns, runs, plans
    for t in [*turns.values(), *runs.values(), *plans.values()]:
        t.cancel()


async def test_stop_all_dry_run_counts_without_stopping(client, running):
    turns, runs, plans = running
    r = await client.post("/api/chat/stop-all", json={"dry_run": True})
    assert r.status_code == 200
    d = r.json()
    assert d["count"] == 5 and d["plans"] == ["alpha"]
    assert sorted(d["conversations"]) == [1, 2, 3, 4]
    await asyncio.sleep(0)
    assert not any(t.cancelled() for t in turns.values())


async def test_stop_all_stops_everything(client, running):
    turns, runs, plans = running
    r = await client.post("/api/chat/stop-all")
    assert r.json()["count"] == 5
    await asyncio.sleep(0)
    assert all(t.cancelled() for t in [*turns.values(), *runs.values(), *plans.values()])


async def test_stop_project_takes_descendants_and_plan_only(client, running):
    turns, runs, plans = running
    r = await client.post("/api/chat/stop-project", json={"project": "alpha"})
    d = r.json()
    assert sorted(d["conversations"]) == [1, 2, 4] and d["plans"] == ["alpha"]
    await asyncio.sleep(0)
    assert turns[1].cancelled() and turns[2].cancelled() and runs[4].cancelled()
    assert not turns[3].cancelled()


async def test_stop_project_unknown_is_404(client, running):
    r = await client.post("/api/chat/stop-project", json={"project": "nope"})
    assert r.status_code == 404


async def test_device_stops_only_its_own_turns(running):
    actor = {"is_device": True, "device_id": 7}
    assert chat_mod._stoppable(actor) == ([3], [], [])
    assert chat_mod._stoppable(actor, {1, 2, 4}, "alpha") == ([], [], [])


async def test_bulk_stop_needs_an_actor(tmp_env):
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.post("/api/chat/stop-all")).status_code == 401
