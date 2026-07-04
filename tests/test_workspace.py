import httpx
import pytest

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
        await c.post("/api/projects", json={"name": "Demo", "summary": "demo"})
        yield c


async def test_file_roundtrip_and_traversal_guard(client):
    r = await client.put("/api/projects/demo/file",
                         json={"path": "notes/idea.md", "content": "# hi"})
    assert r.status_code == 200
    r = await client.get("/api/projects/demo/file", params={"path": "notes/idea.md"})
    assert r.json()["content"] == "# hi"
    r = await client.get("/api/projects/demo/files")
    paths = [f["path"] for f in r.json()["files"]]
    assert "notes/idea.md" in paths and "project.md" in paths

    r = await client.get("/api/projects/demo/file", params={"path": "../../secrets"})
    assert r.status_code == 400
    r = await client.put("/api/projects/demo/file",
                         json={"path": "../evil.md", "content": "x"})
    assert r.status_code == 400


async def test_run_scratch_code_with_artifact(client):
    r = await client.post("/api/projects/demo/run", json={
        "code": "print(2**10)\nopen('out.txt','w').write('artifact')\n"})
    assert r.status_code == 200
    body = r.json()
    assert body["exit_code"] == 0
    assert "1024" in body["stdout"]
    assert "code/out.txt" in body["artifacts"]

    r = await client.post("/api/projects/demo/run", json={"code": "raise ValueError('boom')"})
    body = r.json()
    assert body["exit_code"] != 0 and "boom" in body["stderr"]


async def test_todos(client):
    r = await client.post("/api/projects/demo/todos",
                          json={"action": "add", "text": "write physics sim"})
    assert r.json()["todos"] == [{"done": False, "text": "write physics sim"}]
    r = await client.post("/api/projects/demo/todos", json={"action": "toggle", "index": 0})
    assert r.json()["todos"][0]["done"] is True
    # persisted as a plain markdown checklist the agent can read
    r = await client.get("/api/projects/demo/file", params={"path": "todo.md"})
    assert "- [x] write physics sim" in r.json()["content"]
    r = await client.post("/api/projects/demo/todos", json={"action": "delete", "index": 0})
    assert r.json()["todos"] == []


async def test_memory_api(client):
    r = await client.get("/api/memory")
    names = {f["path"]: f for f in r.json()["files"]}
    assert "soul.md" in names
    assert names["all-projects.md"]["auto_generated"] is True

    r = await client.put("/api/memory/file",
                         json={"path": "notes/facts.md", "content": "remember this"})
    assert r.status_code == 200
    r = await client.get("/api/memory/file", params={"path": "notes/facts.md"})
    assert r.json()["content"] == "remember this"
    r = await client.get("/api/memory/file", params={"path": "../../etc/passwd"})
    assert r.status_code == 400
