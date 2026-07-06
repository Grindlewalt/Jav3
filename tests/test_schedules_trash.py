"""Schedules CRUD + next-run math, agent trash bin, spawn_agent guardrails."""
import datetime as dt

import httpx
import pytest

from backend.agent.tools import registry
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import agents_index, ensure_memory_seeds
from backend.schedules import compute_next


@pytest.fixture
async def client(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "agents_dir", tmp_env / "agents")
    settings.agents_dir.mkdir(parents=True, exist_ok=True)
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


def test_compute_next_daily_rolls_forward():
    after = dt.datetime(2026, 7, 6, 10, 0)
    nxt = compute_next("daily", "08:00", None, after)
    assert nxt == dt.datetime(2026, 7, 7, 8, 0)   # already past today -> tomorrow
    nxt = compute_next("daily", "18:00", None, after)
    assert nxt == dt.datetime(2026, 7, 6, 18, 0)  # later today


def test_compute_next_interval_has_floor():
    after = dt.datetime(2026, 7, 6, 10, 0)
    assert compute_next("interval", None, 5, after) == after + dt.timedelta(minutes=15)
    assert compute_next("interval", None, 120, after) == after + dt.timedelta(minutes=120)


async def test_schedule_crud(client):
    r = await client.post("/api/schedules", json={
        "name": "morning", "kind": "jarvis", "task": "summarize the day",
        "cadence_kind": "daily", "daily_at": "07:30"})
    assert r.status_code == 200
    sid = r.json()["id"]
    r = await client.get("/api/schedules")
    assert any(s["id"] == sid and s["enabled"] for s in r.json()["schedules"])
    await client.patch(f"/api/schedules/{sid}?enabled=false")
    r = await client.get("/api/schedules")
    assert next(s for s in r.json()["schedules"] if s["id"] == sid)["enabled"] == 0
    await client.delete(f"/api/schedules/{sid}")
    r = await client.get("/api/schedules")
    assert all(s["id"] != sid for s in r.json()["schedules"])


async def test_schedule_validation(client):
    r = await client.post("/api/schedules", json={
        "name": "bad", "kind": "agent", "task": "x", "cadence_kind": "daily"})
    assert r.status_code == 400  # agent kind without agent_slug


async def test_agent_trash_lifecycle(client):
    await client.post("/api/agents", json={"name": "Scout"})
    assert any(a["slug"] == "scout" for a in (await client.get("/api/agents")).json()["agents"])
    # soft delete -> gone from list, present in trash
    await client.delete("/api/agents/scout")
    assert all(a["slug"] != "scout" for a in (await client.get("/api/agents")).json()["agents"])
    assert any(a["slug"] == "scout" for a in (await client.get("/api/agents/trash")).json()["agents"])
    # trashed agent is not in Jarvis's roster
    assert "scout" not in agents_index()
    # restore
    await client.post("/api/agents/scout/restore")
    assert any(a["slug"] == "scout" for a in (await client.get("/api/agents")).json()["agents"])
    # delete + purge
    await client.delete("/api/agents/scout")
    r = await client.delete("/api/agents/scout/purge")
    assert r.status_code == 200
    assert (await client.get("/api/agents/trash")).json()["agents"] == []


async def test_purge_requires_trash_first(client):
    await client.post("/api/agents", json={"name": "Live"})
    r = await client.delete("/api/agents/live/purge")
    assert r.status_code == 400  # still live, not in trash


async def test_spawn_agent_registered_and_guards(tmp_env):
    names = {e["name"] for e in registry.compile_registry()}
    assert "spawn_agent" in names
    out = await registry.dispatch("spawn_agent", {"agent": "ghost", "task": "hi"})
    assert "no agent named 'ghost'" in out


async def test_agent_model_override_plumbed(client, monkeypatch):
    # an agent with a model + ollama base_url should push those into model.complete
    await client.post("/api/agents", json={"name": "Local"})
    await client.put("/api/agents/local", json={
        "name": "Local", "model": "qwen3", "base_url": "http://localhost:11434/v1",
        "prompt": "you are local", "context_exclude": [], "tools_exclude": [],
        "skills_exclude": []})
    from backend.agents_run import _read, _agent_overrides
    mdl, burl = _agent_overrides(_read("local"))
    assert mdl == "qwen3" and burl == "http://localhost:11434/v1"
    # a default agent inherits (None, None)
    await client.post("/api/agents", json={"name": "Default"})
    mdl2, burl2 = _agent_overrides(_read("default"))
    assert mdl2 is None and burl2 is None
