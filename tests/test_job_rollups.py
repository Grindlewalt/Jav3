"""ROBUST-22: a funnel or research job head whose run fails or is stopped still
gets a rollup. The chat reload payload marks a head `running` while its rollup
is NULL, so a head that never got one showed a live JobTree that never ended."""
import asyncio
import contextlib

import pytest

from backend import orchestrator, research
from backend.db import get_db, init_db

async def _head_rollup(job_id: str):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT id, rollup FROM conversations WHERE job_id = ? AND kind = 'head'",
            (job_id,)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert len(rows) == 1, rows
    return rows[0]["rollup"]


@pytest.fixture
async def env(tmp_env, monkeypatch):
    await init_db()

    @contextlib.asynccontextmanager
    async def no_workspace(project, *, top_level):
        yield
    monkeypatch.setattr(orchestrator, "job_workspace", no_workspace)


async def test_a_funnel_head_that_fails_gets_an_error_rollup(env, monkeypatch):
    async def boom(brief, kind):
        raise RuntimeError("planner unreachable")
    monkeypatch.setattr(orchestrator, "_decompose", boom)
    out = await orchestrator.run_job("job-fail", "do a thing", "")
    assert out["rollup"] == "error: planner unreachable"
    assert await _head_rollup("job-fail") == "error: planner unreachable"


async def test_a_funnel_head_that_is_stopped_gets_a_stopped_rollup(env, monkeypatch):
    gate = asyncio.Event()

    async def slow(brief, kind):
        await gate.wait()
    monkeypatch.setattr(orchestrator, "_decompose", slow)
    t = asyncio.create_task(orchestrator.run_job("job-stop", "do a thing", ""))
    for _ in range(100):
        await asyncio.sleep(0.01)
        db = await get_db()
        try:
            async with db.execute("SELECT 1 FROM conversations WHERE job_id = 'job-stop'") as cur:
                if await cur.fetchone():
                    break
        finally:
            await db.close()
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert await _head_rollup("job-stop") == "stopped"


async def test_a_research_head_that_is_stopped_gets_a_stopped_rollup(env, monkeypatch):
    gate = asyncio.Event()

    async def slow(topic):
        await gate.wait()
    monkeypatch.setattr(research, "_gen_queries", slow)
    t = asyncio.create_task(research.run_research("a topic", "", job_id="job-rs"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        db = await get_db()
        try:
            async with db.execute("SELECT 1 FROM conversations WHERE job_id = 'job-rs'") as cur:
                if await cur.fetchone():
                    break
        finally:
            await db.close()
    await asyncio.sleep(0.1)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert await _head_rollup("job-rs") == "stopped"


async def test_a_head_lost_in_a_restart_is_settled_at_boot(env):
    from backend.db import open_conversation
    db = await get_db()
    try:
        await open_conversation(db, project=None, title="[head] x", kind="head",
                                job_id="job-lost")
        done = await open_conversation(db, project=None, title="[head] y", kind="head",
                                       job_id="job-done")
        await db.execute("UPDATE conversations SET rollup = 'fine' WHERE id = ?", (done,))
        await db.commit()
    finally:
        await db.close()
    assert await orchestrator.settle_lost_heads() == 1
    assert "interrupted" in await _head_rollup("job-lost")
    assert await _head_rollup("job-done") == "fine"
