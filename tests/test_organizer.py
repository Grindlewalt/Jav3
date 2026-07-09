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
        await c.post("/api/projects", json={"name": "Demo", "summary": "demo"})
        yield c


async def test_mkdir_marks_move(client):
    r = await client.post("/api/projects/demo/mkdir",
                          json={"path": "images", "mark": "rendered plots and figures"})
    assert r.status_code == 200
    r = await client.put("/api/projects/demo/file",
                         json={"path": "plot.png.txt", "content": "fake"})
    r = await client.post("/api/projects/demo/move",
                          json={"src": "plot.png.txt", "dest": "images/plot.png.txt"})
    assert r.status_code == 200
    r = await client.get("/api/projects/demo/dirs")
    dirs = {d["path"]: d["mark"] for d in r.json()["dirs"]}
    assert dirs["images"] == "rendered plots and figures"
    assert "" in dirs  # project root is a target too
    r = await client.get("/api/projects/demo/files")
    paths = [f["path"] for f in r.json()["files"]]
    assert "images/plot.png.txt" in paths and "plot.png.txt" not in paths
    # marks are invisible to the file list
    assert not any(".about.md" in p for p in paths)

    # project.md is pinned; moves that escape the project are rejected
    r = await client.post("/api/projects/demo/move",
                          json={"src": "project.md", "dest": "images/project.md"})
    assert r.status_code == 400
    r = await client.post("/api/projects/demo/move",
                          json={"src": "images/plot.png.txt", "dest": "../escape.txt"})
    assert r.status_code == 400

    r = await client.put("/api/projects/demo/dirs/mark",
                         json={"path": "images", "mark": ""})
    assert r.status_code == 200
    r = await client.post("/api/projects/demo/move",
                          json={"src": "images/plot.png.txt", "dest": "plot.png.txt"})
    r = await client.delete("/api/projects/demo/dirs", params={"path": "images"})
    assert r.status_code == 200


async def test_layout_roundtrip(client):
    r = await client.get("/api/projects/demo/layout")
    assert r.json()["layout"] is None
    layout = {"panels": [{"id": "p1", "type": "todos", "x": 20, "y": 30, "w": 300, "h": 280}]}
    r = await client.put("/api/projects/demo/layout", json=layout)
    assert r.status_code == 200
    r = await client.get("/api/projects/demo/layout")
    assert r.json()["layout"] == layout
    # layout file is hidden from the file list
    r = await client.get("/api/projects/demo/files")
    assert not any(".workspace" in f["path"] for f in r.json()["files"])


async def test_skills_and_tools(client):
    r = await client.post("/api/skills",
                          json={"name": "Organize Project", "description": "test dup guard"})
    slug = r.json()["slug"]
    assert slug == "organize-project"
    r = await client.get("/api/skills")
    skills = {s["slug"]: s for s in r.json()["skills"]}
    assert skills[slug]["enabled"] is True   # granted by default (operator, 2026-07-09)

    r = await client.get(f"/api/skills/{slug}")
    assert "(instructions the model follows" in r.json()["content"]
    new = r.json()["content"].replace("(describe what this skill does)", "organizes files")
    r = await client.put(f"/api/skills/{slug}", json={"content": new})
    assert r.status_code == 200

    # untick "granted" via the fields editor -> catalogued in /api/tools but
    # NOT granted to the model
    r = await client.get(f"/api/skills/{slug}")
    fields = {**r.json()["fields"], "enabled": False}
    r = await client.put(f"/api/skills/{slug}/fields", json=fields)
    assert r.status_code == 200
    r = await client.get("/api/tools")
    tools = {t["name"]: t for t in r.json()["tools"]}
    assert tools["organize-project"]["enabled"] is False
    from backend.agent.tools.registry import load_registry, openai_tool_specs
    assert all(s["function"]["name"] != "organize-project"
               for s in openai_tool_specs(load_registry()))
