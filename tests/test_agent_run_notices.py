"""Interactive agent runs: detached from the HTTP connection, and announced.

Before this, the run WAS the response generator — closing the panel or leaving
the page cancelled it and the agent's work died silently. So "tell me when the
agent finishes if I clicked off" needed the run to survive being clicked off
first; these tests pin both halves, plus the rule about which runs announce
themselves (operator-started named agents only, never Jarvis's spawned ones).

Technique borrowed from test_background_chat: httpx's ASGITransport buffers a
streaming response until the app finishes, so a run is driven as an asyncio
task and blocked on an event rather than read as live SSE.
"""
import asyncio
import contextlib

import httpx
import pytest

from backend import bus
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    settings.agents_dir.mkdir(parents=True, exist_ok=True)
    d = settings.agents_dir / "scout"
    d.mkdir(exist_ok=True)
    (d / "AGENT.md").write_text(
        "---\nname: Scout\ndescription: finds things\n---\nYou are Scout.\n")
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


def _blocked_turn(release: asyncio.Event, started: asyncio.Event, text: str):
    async def turn(cid, system_prompt, history, **kw):
        started.set()
        await release.wait()
        yield {"type": "final", "content": text}
    return turn


async def _start_run(client, monkeypatch, text="scouted"):
    from backend import agents_run
    release, started = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(agents_run, "run_agent_turn", _blocked_turn(release, started, text))
    post = asyncio.create_task(client.post(
        "/api/agents/scout/run", json={"task": "look around", "confirm_peak": True}))
    await asyncio.wait_for(started.wait(), 5)
    return post, max(agents_run._active_runs), release


async def _drain(cid):
    from backend import agents_run
    task = agents_run._active_runs.get(cid)
    if task:
        await task


async def test_run_survives_the_operator_clicking_off(client, monkeypatch):
    """The whole feature rests on this: leaving must not kill the agent."""
    post, cid, release = await _start_run(client, monkeypatch, "finished alone")
    post.cancel()                                   # they navigated away
    with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
        await post
    release.set()
    await _drain(cid)

    db = await get_db()
    try:
        async with db.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? "
                "ORDER BY id", (cid,)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert rows[-1]["role"] == "assistant"
    assert rows[-1]["content"] == "finished alone"


async def test_finished_run_announces_itself(client, monkeypatch):
    q = bus.subscribe("agent_notices")
    try:
        post, cid, release = await _start_run(client, monkeypatch, "the report body")
        release.set()
        await asyncio.wait_for(post, 5)
        await _drain(cid)
        ev = await asyncio.wait_for(q.get(), 5)
    finally:
        bus.unsubscribe("agent_notices", q)

    assert ev["type"] == "agent_run_done"
    assert ev["conversation_id"] == cid
    assert ev["agent"] == "Scout" and ev["ok"] is True
    assert "the report body" in ev["summary"]
    assert ev["took"]


async def test_a_failed_run_still_announces(client, monkeypatch):
    """A run that died after the operator walked away is the case most worth
    telling them about, so the notice must not be success-only."""
    from backend import agents_run

    def boom(*a, **kw):
        async def gen():
            raise RuntimeError("model exploded")
            yield  # pragma: no cover
        return gen()

    monkeypatch.setattr(agents_run, "run_agent_turn", boom)
    q = bus.subscribe("agent_notices")
    try:
        r = await client.post("/api/agents/scout/run",
                              json={"task": "go", "confirm_peak": True})
        assert r.status_code == 200
        ev = await asyncio.wait_for(q.get(), 5)
    finally:
        bus.unsubscribe("agent_notices", q)
    assert ev["ok"] is False and "model exploded" in ev["error"]


async def test_spawned_and_temp_agents_never_announce(client, monkeypatch):
    """Only the operator-started path publishes. Jarvis spawns named and temp
    agents constantly inside a turn that is already on screen; toasting those
    would bury the one run the operator actually walked away from."""
    from backend import agents_run

    async def fake_turn(cid, system_prompt, history, **kw):
        yield {"type": "final", "content": "child done"}

    monkeypatch.setattr(agents_run, "run_agent_turn", fake_turn)
    q = bus.subscribe("agent_notices")
    try:
        await agents_run.run_agent_headless("scout", "background work")
        await agents_run.run_temp_agent_headless("be useful", "a temp task")
        await asyncio.sleep(0.05)
        assert q.empty(), "a spawned agent must not toast the operator"
    finally:
        bus.unsubscribe("agent_notices", q)


async def test_resume_stream_reattaches_then_idles(client, monkeypatch):
    post, cid, release = await _start_run(client, monkeypatch, "whole reply")
    tail = asyncio.create_task(client.get(f"/api/agents/runs/{cid}/stream"))
    await asyncio.sleep(0.05)          # let the tail subscribe
    release.set()
    r = await asyncio.wait_for(tail, 5)
    assert '"final"' in r.text and "whole reply" in r.text
    await asyncio.wait_for(post, 5)
    await _drain(cid)
    # and once it is over, re-attaching says so instead of hanging
    r = await client.get(f"/api/agents/runs/{cid}/stream")
    assert '"idle"' in r.text
