"""ROBUST-18: the heartbeat claims a due schedule (next_run advanced, last_result
marked running) BEFORE it runs it, runs due schedules as concurrent tracked
tasks, and run-now is a background start with a guard against a second run."""
import asyncio
import datetime as dt

import httpx
import pytest

from backend import schedules
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app


@pytest.fixture
async def client(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "agents_dir", tmp_env / "agents")
    settings.agents_dir.mkdir(parents=True, exist_ok=True)
    await init_db()
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
    for t in list(schedules._running.values()):
        t.cancel()
    schedules._running.clear()


async def _make(client, name: str, **over) -> int:
    body = {"name": name, "kind": "jarvis", "task": f"do {name}",
            "cadence_kind": "interval", "interval_minutes": 60}
    body.update(over)
    r = await client.post("/api/schedules", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _sql(query: str, args: tuple = ()) -> list[dict]:
    db = await get_db()
    try:
        async with db.execute(query, args) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        await db.commit()
        return rows
    finally:
        await db.close()


async def _make_due(sid: int) -> str:
    past = (schedules._now() - dt.timedelta(minutes=5)).isoformat(timespec="minutes")
    await _sql("UPDATE schedules SET next_run = ? WHERE id = ?", (past, sid))
    return past


async def test_a_due_schedule_is_claimed_before_it_runs_and_runs_do_not_queue(
        client, monkeypatch):
    started, release = [], asyncio.Event()

    async def fake_run(row):
        started.append(row["name"])
        await release.wait()
        return "ok"
    monkeypatch.setattr(schedules, "_run_schedule", fake_run)
    digest, backup = await _make(client, "digest"), await _make(client, "backup")
    old_a, old_b = await _make_due(digest), await _make_due(backup)

    await schedules._tick()
    await asyncio.sleep(0.05)
    # one slow run does not hold the others up
    assert sorted(started) == ["backup", "digest"], started
    # claimed: next_run is already in the future and the row says it is running
    rows = {r["id"]: r for r in await _sql("SELECT * FROM schedules")}
    for sid, old in ((digest, old_a), (backup, old_b)):
        assert rows[sid]["next_run"] > old
        assert rows[sid]["next_run"] > schedules._now().isoformat(timespec="minutes")
        assert rows[sid]["last_result"] == schedules.RUNNING
    # a tick while they run starts nothing twice
    await schedules._tick()
    await asyncio.sleep(0.02)
    assert len(started) == 2

    release.set()
    await asyncio.gather(*schedules._running.values())
    rows = {r["id"]: r for r in await _sql("SELECT * FROM schedules")}
    assert rows[digest]["last_result"] == "ok" and rows[backup]["last_result"] == "ok"
    assert not schedules._running


async def test_a_restart_mid_run_does_not_repeat_it(client, monkeypatch):
    gate = asyncio.Event()

    async def fake_run(row):
        await gate.wait()
        return "never"
    monkeypatch.setattr(schedules, "_run_schedule", fake_run)
    sid = await _make(client, "long")
    await _make_due(sid)
    await schedules._tick()
    await asyncio.sleep(0.02)
    for t in list(schedules._running.values()):      # the process goes away
        t.cancel()
    await asyncio.gather(*schedules._running.values(), return_exceptions=True)
    schedules._running.clear()
    row = (await _sql("SELECT * FROM schedules WHERE id = ?", (sid,)))[0]
    assert row["next_run"] > schedules._now().isoformat(timespec="minutes")

    ran = []

    async def fake_run2(row):
        ran.append(row["id"])
        return "ok"
    monkeypatch.setattr(schedules, "_run_schedule", fake_run2)
    await schedules._tick()                          # the first tick after boot
    await asyncio.sleep(0.02)
    assert ran == [], "the interrupted run was started again"
    # the boot sweep says what happened instead of leaving 'running' for good
    await schedules._mark_interrupted()
    row = (await _sql("SELECT last_result FROM schedules WHERE id = ?", (sid,)))[0]
    assert "interrupted" in row["last_result"]


async def test_a_schedule_disabled_or_deleted_after_the_snapshot_does_not_run(
        client, monkeypatch):
    ran = []

    async def fake_run(row):
        ran.append(row["id"])
        return "ok"
    monkeypatch.setattr(schedules, "_run_schedule", fake_run)
    off, gone = await _make(client, "off"), await _make(client, "gone")
    for sid in (off, gone):
        await _make_due(sid)
    rows = await _sql("SELECT * FROM schedules")       # the tick's snapshot
    await _sql("UPDATE schedules SET enabled = 0 WHERE id = ?", (off,))
    await _sql("UPDATE schedules SET deleted_at = datetime('now') WHERE id = ?", (gone,))
    for row in rows:
        assert await schedules._claim(row) is False
    assert ran == []


async def test_run_now_starts_in_the_background_and_refuses_a_second_run(
        client, monkeypatch):
    release = asyncio.Event()
    runs = []

    async def fake_run(row):
        runs.append(row["id"])
        await release.wait()
        return "done"
    monkeypatch.setattr(schedules, "_run_schedule", fake_run)
    sid = await _make(client, "manual")
    before = (await _sql("SELECT next_run FROM schedules WHERE id = ?", (sid,)))[0]["next_run"]
    r = await asyncio.wait_for(client.post(f"/api/schedules/{sid}/run-now"), 2)
    assert r.status_code == 202, r.text                # returned while it is still running
    r2 = await client.post(f"/api/schedules/{sid}/run-now")
    assert r2.status_code == 409 and "already running" in r2.json()["detail"]
    row = (await _sql("SELECT * FROM schedules WHERE id = ?", (sid,)))[0]
    assert row["last_result"] == schedules.RUNNING and row["next_run"] == before
    release.set()
    await asyncio.gather(*schedules._running.values())
    row = (await _sql("SELECT * FROM schedules WHERE id = ?", (sid,)))[0]
    assert runs == [sid] and row["last_result"] == "done" and row["next_run"] == before
