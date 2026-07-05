import httpx
import pytest

from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds, read_memory_file


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
        await c.post("/api/projects", json={"name": "Demo", "summary": "a demo"})
        yield c


async def _new_conversation(client) -> int:
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (project_id) VALUES (NULL)")
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def test_conversation_delete_and_assign(client):
    cid = await _new_conversation(client)
    r = await client.patch(f"/api/conversations/{cid}", json={"project": "demo"})
    assert r.status_code == 200
    r = await client.get("/api/conversations")
    convo = next(c for c in r.json()["conversations"] if c["id"] == cid)
    assert convo["project_slug"] == "demo"

    r = await client.patch(f"/api/conversations/{cid}", json={"project": None})
    assert r.status_code == 200
    r = await client.patch(f"/api/conversations/{cid}", json={"project": "nope"})
    assert r.status_code == 404

    r = await client.delete(f"/api/conversations/{cid}")
    assert r.status_code == 200
    r = await client.get("/api/conversations")
    assert all(c["id"] != cid for c in r.json()["conversations"])


async def test_project_bin_flow(client):
    await client.post("/api/projects/demo/load")

    r = await client.delete("/api/projects/demo")
    assert r.status_code == 200
    r = await client.get("/api/projects")
    body = r.json()
    assert body["projects"] == []
    assert body["deleted"][0]["slug"] == "demo"
    assert body["active"] is None                      # unloaded on delete
    assert "Demo" not in read_memory_file("all-projects.md")
    # can't load or re-delete a binned project
    assert (await client.post("/api/projects/demo/load")).status_code == 404
    assert (await client.delete("/api/projects/demo")).status_code == 404

    r = await client.post("/api/projects/demo/restore")
    assert r.status_code == 200
    r = await client.get("/api/projects")
    assert r.json()["projects"][0]["slug"] == "demo"
    assert "Demo" in read_memory_file("all-projects.md")

    # purge requires the bin
    assert (await client.delete("/api/projects/demo/purge")).status_code == 400
    await client.delete("/api/projects/demo")
    r = await client.delete("/api/projects/demo/purge")
    assert r.status_code == 200
    assert not (settings.projects_dir / "demo").exists()
    r = await client.get("/api/projects")
    assert r.json()["projects"] == [] and r.json()["deleted"] == []
