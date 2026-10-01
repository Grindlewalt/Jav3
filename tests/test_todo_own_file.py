"""BUILD-07 / BUILD-15: todo_update keeps the agent's list in its own hidden
file, never rewrites a project's todo.md, and reports what changed instead of
echoing the whole list on every call."""

import httpx
import pytest

from backend.agent.tools import registry
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds

REAL = "# Roadmap\n\nProse the operator wrote.\n\n## Next\n- [ ] ship 1.0\n- [x] write docs\n\nA trailing note.\n"


@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        await c.post("/api/projects", json={"name": "Demo", "summary": "demo"})
        await c.post("/api/projects/demo/load")
        yield c


def _dir():
    return settings.projects_dir / "demo"


async def _todo(**kw):
    return await registry.dispatch("todo_update", kw)


async def test_a_real_todo_md_is_never_rewritten(client):
    (_dir() / "todo.md").write_text(REAL)
    await _todo(action="add", text="new item")
    await _todo(action="check", text="new item")
    assert (_dir() / "todo.md").read_text() == REAL
    assert "- [x] new item" in (_dir() / ".todo.md").read_text()


async def test_removing_todo_md_does_not_lose_the_list(client):
    await _todo(action="add", text="one")
    (_dir() / "todo.md").unlink(missing_ok=True)
    out = await _todo(action="check", text="one")
    assert out.startswith("checked") and "[x] one" in out
    assert "one" in await _todo(action="list")


async def test_an_existing_todo_md_seeds_the_list_once(client):
    (_dir() / "todo.md").write_text(REAL)
    assert "ship 1.0" in await _todo(action="list")
    await _todo(action="delete", text="ship 1.0")
    # the own file now rules: the delete is not undone by todo.md's old line
    assert "ship 1.0" not in await _todo(action="list")
    assert (_dir() / "todo.md").read_text() == REAL


async def test_the_board_reads_and_writes_the_same_list(client):
    await _todo(action="add", text="from the agent")
    r = await client.get("/api/projects/demo/todos")
    assert r.json()["todos"] == [{"done": False, "text": "from the agent"}]
    r = await client.post("/api/projects/demo/todos", json={"action": "add", "text": "from the board"})
    assert [t["text"] for t in r.json()["todos"]] == ["from the agent", "from the board"]
    assert "from the board" in await _todo(action="list")


async def test_add_takes_several_items_and_answers_with_the_change_only(client):
    await _todo(action="add", text="first")
    out = await _todo(action="add", items=["second", "third"])
    assert "added 2" in out and "1. [ ] second" in out and "2. [ ] third" in out
    assert "first" not in out, "the whole list is not echoed on every call"
    assert "3 items" in out
    # list still shows everything
    assert "0. [ ] first" in await _todo(action="list")


async def test_check_answers_with_the_one_item_and_the_progress(client):
    await _todo(action="add", items=["a thing", "another"])
    out = await _todo(action="check", text="another")
    assert "1. [x] another" in out and "a thing" not in out and "2 items, 1 done" in out
