"""M7: event bus, claim-based fetch ledger, orchestrator caps + tree wiring.
Full research flow needs the live API, so it's verified on the Pi."""
import asyncio

import httpx
import pytest

from backend import bus, orchestrator, webtools
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


# --- event bus ---------------------------------------------------------------

async def test_bus_fanout_and_unsubscribe():
    q1 = bus.subscribe("job1")
    q2 = bus.subscribe("job1")
    bus.publish("job1", {"type": "x", "n": 1})
    assert (await q1.get())["n"] == 1
    assert (await q2.get())["n"] == 1
    bus.unsubscribe("job1", q1)
    bus.publish("job1", {"type": "x", "n": 2})
    assert (await q2.get())["n"] == 2
    assert q1.empty()
    bus.unsubscribe("job1", q2)
    # no subscribers left -> publish is a harmless no-op
    bus.publish("job1", {"type": "x", "n": 3})


async def test_bus_bounded_drops_oldest():
    q = bus.subscribe("job2")
    for i in range(1100):  # maxsize is 1000
        bus.publish("job2", {"type": "token", "i": i})
    assert q.qsize() <= 1000
    first = await q.get()
    assert first["i"] > 0  # oldest were dropped
    bus.unsubscribe("job2", q)


async def test_bus_close_job_sentinel():
    q = bus.subscribe("job3")
    bus.close_job("job3")
    assert (await q.get()) is bus.JOB_END
    bus.unsubscribe("job3", q)


# --- claim-based fetch ledger (parallel-safe dedup) --------------------------

async def test_claim_is_exclusive(tmp_env):
    await init_db()
    # two concurrent claims of the same url -> exactly one wins
    results = await asyncio.gather(
        webtools.claim("sess", "https://a.com/x"),
        webtools.claim("sess", "https://a.com/x"),
        webtools.claim("sess", "https://a.com/x"))
    assert sum(results) == 1
    # a different url is claimable
    assert await webtools.claim("sess", "https://a.com/y") is True
    # release lets it be reclaimed
    await webtools.release("sess", "https://a.com/x")
    assert await webtools.claim("sess", "https://a.com/x") is True


async def test_read_refused_url_leaves_no_claim(tmp_env):
    await init_db()
    out = await webtools.read("http://169.254.169.254/", "sess")
    assert out.startswith("error: refused")
    assert await webtools.fetched_set("sess") == set()  # nothing claimed


# --- orchestrator caps + decompose parsing -----------------------------------

def test_budget_caps():
    b = orchestrator._Budget(2)
    assert b.take() and b.take()
    assert not b.take()          # exhausted
    assert b.remaining() == 0


def test_caps_are_sane():
    assert orchestrator.MAX_DEPTH >= 1
    assert orchestrator.MAX_NODES >= orchestrator.MAX_FANOUT


# --- API surface: runs endpoints registered ----------------------------------

@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("pw")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "pw"})
        yield c


async def test_runs_endpoints(client):
    r = await client.get("/api/runs")
    assert r.status_code == 200 and r.json()["runs"] == []
    r = await client.get("/api/runs/999/tree")
    assert r.status_code == 404
    # research without a loaded project is rejected
    r = await client.post("/api/runs/research", json={"topic": "x"})
    assert r.status_code == 400


async def test_job_nodes_hidden_from_chat_list(client):
    db = await get_db()
    try:
        await db.execute("INSERT INTO conversations (summary, kind) VALUES ('a chat', 'chat')")
        await db.execute("INSERT INTO conversations (summary, kind, job_id) VALUES ('a head', 'head', 'j1')")
        await db.execute("INSERT INTO conversations (summary, kind, job_id) VALUES ('a sub', 'subagent', 'j1')")
        await db.commit()
    finally:
        await db.close()
    r = await client.get("/api/conversations")
    summaries = [c["summary"] for c in r.json()["conversations"]]
    assert "a chat" in summaries
    assert "a head" not in summaries and "a sub" not in summaries
