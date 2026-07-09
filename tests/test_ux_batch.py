"""UX batch: schedule edit endpoint, memory/debug-context token counts."""
import httpx
import pytest

from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
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
            ("operator", hash_password("hunter2")),
        )
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


async def test_schedule_edit_roundtrip(client):
    r = await client.post("/api/schedules", json={
        "name": "morning", "task": "say hi", "cadence_kind": "daily",
        "daily_at": "08:00"})
    sid = r.json()["id"]
    r = await client.put(f"/api/schedules/{sid}", json={
        "name": "evening", "task": "say bye", "cadence_kind": "daily",
        "daily_at": "20:15"})
    assert r.status_code == 200
    assert r.json()["next_run"].endswith("20:15")
    r = await client.get("/api/schedules")
    s = next(s for s in r.json()["schedules"] if s["id"] == sid)
    assert (s["name"], s["task"], s["daily_at"]) == ("evening", "say bye", "20:15")


async def test_schedule_edit_validates_and_404s(client):
    r = await client.put("/api/schedules/424242", json={
        "name": "x", "task": "y", "cadence_kind": "daily", "daily_at": "09:00"})
    assert r.status_code == 404
    r = await client.post("/api/schedules", json={
        "name": "a", "task": "b", "cadence_kind": "daily", "daily_at": "09:00"})
    sid = r.json()["id"]
    r = await client.put(f"/api/schedules/{sid}", json={
        "name": "a", "task": "b", "cadence_kind": "hourly"})
    assert r.status_code == 400


async def test_memory_list_has_token_counts(client):
    (settings.memory_dir / "notes").mkdir(parents=True, exist_ok=True)
    (settings.memory_dir / "notes" / "n.md").write_text("word " * 100)
    r = await client.get("/api/memory")
    by_path = {f["path"]: f for f in r.json()["files"]}
    assert by_path["notes/n.md"]["tokens"] == 125   # 500 chars / 4


async def test_debug_context_reports_tokens(client):
    r = await client.get("/api/debug/context")
    body = r.json()
    assert body["tokens"] > 0
    assert abs(body["tokens"] - len(body["system_prompt"]) / 4) < 2
