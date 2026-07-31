"""Deleting a schedule is a soft delete: it stops running now, comes back on
request, and the bin empties itself past its window."""
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


async def _make(client, name: str, **over) -> int:
    body = {"name": name, "kind": "jarvis", "task": f"do {name}",
            "cadence_kind": "daily", "daily_at": "07:30"}
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


async def _make_due(sid: int) -> None:
    """Backdate next_run so the heartbeat considers this one due."""
    past = (schedules._now() - dt.timedelta(minutes=5)).isoformat(timespec="minutes")
    await _sql("UPDATE schedules SET next_run = ? WHERE id = ?", (past, sid))


async def _backdate_delete(sid: int, days: int) -> None:
    await _sql("UPDATE schedules SET deleted_at = datetime('now', ?) WHERE id = ?",
               (f"-{days} days", sid))


async def test_delete_is_soft_and_restorable(client):
    sid = await _make(client, "morning")
    r = await client.delete(f"/api/schedules/{sid}")
    assert r.status_code == 200
    body = (await client.get("/api/schedules")).json()
    assert all(s["id"] != sid for s in body["schedules"])
    binned = [s for s in body["deleted"] if s["id"] == sid]
    assert len(binned) == 1 and binned[0]["name"] == "morning"
    assert binned[0]["deleted_at"]
    # the row is still there, just flagged
    assert len(await _sql("SELECT id FROM schedules WHERE id = ?", (sid,))) == 1

    r = await client.post(f"/api/schedules/{sid}/restore")
    assert r.status_code == 200
    body = (await client.get("/api/schedules")).json()
    assert any(s["id"] == sid for s in body["schedules"])
    assert body["deleted"] == []


async def test_deleted_schedule_stops_running(client, monkeypatch):
    ran = []

    async def fake_run(row):
        ran.append(row["id"])
        return "ok"

    monkeypatch.setattr(schedules, "_run_schedule", fake_run)
    live = await _make(client, "live")
    doomed = await _make(client, "doomed")
    await _make_due(live)
    await _make_due(doomed)
    await client.delete(f"/api/schedules/{doomed}")
    await schedules._tick()
    assert ran == [live]


async def test_restore_pushes_next_run_forward(client):
    """A schedule that sat in the bin past its next_run must not fire the
    instant it is restored."""
    sid = await _make(client, "hourly", cadence_kind="interval",
                      interval_minutes=60)
    await _make_due(sid)
    await client.delete(f"/api/schedules/{sid}")
    await client.post(f"/api/schedules/{sid}/restore")
    row = (await _sql("SELECT next_run FROM schedules WHERE id = ?", (sid,)))[0]
    assert row["next_run"] > schedules._now().isoformat(timespec="minutes")


async def test_bin_is_time_bounded_and_swept(client):
    fresh = await _make(client, "fresh")
    old = await _make(client, "old")
    await client.delete(f"/api/schedules/{fresh}")
    await client.delete(f"/api/schedules/{old}")
    await _backdate_delete(old, schedules.TRASH_DAYS + 10)
    body = (await client.get("/api/schedules")).json()
    assert [s["id"] for s in body["deleted"]] == [fresh]
    # ...and the sweep drops the out-of-window row for real
    await schedules._sweep_trash()
    ids = {r["id"] for r in await _sql("SELECT id FROM schedules")}
    assert fresh in ids and old not in ids


async def test_deleted_schedule_refuses_edits_and_runs(client):
    sid = await _make(client, "gone")
    await client.delete(f"/api/schedules/{sid}")
    assert (await client.post(f"/api/schedules/{sid}/run-now")).status_code == 404
    r = await client.put(f"/api/schedules/{sid}", json={
        "name": "gone", "kind": "jarvis", "task": "changed",
        "cadence_kind": "daily", "daily_at": "08:00"})
    assert r.status_code == 404
    # toggling a binned schedule is a no-op, not a resurrection
    await client.patch(f"/api/schedules/{sid}?enabled=true")
    assert (await client.get("/api/schedules")).json()["schedules"] == []
    # second delete has nothing to delete
    assert (await client.delete(f"/api/schedules/{sid}")).status_code == 404


async def test_purge_requires_the_bin_first(client):
    sid = await _make(client, "keeper")
    assert (await client.delete(f"/api/schedules/{sid}/purge")).status_code == 400
    await client.delete(f"/api/schedules/{sid}")
    assert (await client.delete(f"/api/schedules/{sid}/purge")).status_code == 200
    assert await _sql("SELECT id FROM schedules WHERE id = ?", (sid,)) == []
    assert (await client.post(f"/api/schedules/{sid}/restore")).status_code == 404


async def test_delete_clears_a_pending_proposal_from_the_bell(client):
    """A Jarvis-proposed schedule sits in the bell until decided; deleting it
    IS a decision, so the bell must stop counting it."""
    sid = await _make(client, "proposed")
    await _sql("UPDATE schedules SET enabled = 0, pending_approval = 1 "
               "WHERE id = ?", (sid,))
    body = (await client.get("/api/notifications")).json()
    assert [s["id"] for s in body["schedules"]] == [sid]
    await client.delete(f"/api/schedules/{sid}")
    body = (await client.get("/api/notifications")).json()
    assert body["schedules"] == []
