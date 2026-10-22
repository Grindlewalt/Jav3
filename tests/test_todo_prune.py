"""A long todo list sheds its finished items to .todo-archive.md and shows at most
40 open items: an orchestrator's list reached 149 items (11.5 KB) and rode along
in every `list` result."""

import httpx
import pytest

from backend.agent.tools import registry
from backend.agent.tools.todostore import (DONE_KEEP, OPEN_VIEW_CAP, PRUNE_ABOVE,
                                           archive_text, parse_todo_text, prune,
                                           render_todos)
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


def _items(n, done=()):
    return [{"done": i in done, "text": f"task-{i:03d}"} for i in range(n)]


def test_a_short_list_keeps_its_ticks():
    todos = _items(PRUNE_ABOVE, done=range(30))
    kept, gone = prune(todos)
    assert kept == todos and gone == []


def test_a_long_list_keeps_every_open_item_and_the_latest_finished():
    todos = _items(PRUNE_ABOVE + 20, done=range(0, 40, 2))      # 20 done, 65 items
    kept, gone = prune(todos)
    assert [t["text"] for t in gone] == [f"task-{i:03d}" for i in range(0, 30, 2)]
    assert all(t["done"] for t in gone) and len(gone) == 20 - DONE_KEEP
    assert sum(1 for t in kept if t["done"]) == DONE_KEEP
    assert [t for t in kept if not t["done"]] == [t for t in todos if not t["done"]]
    assert [t["text"] for t in kept if t["done"]] == [f"task-{i:03d}" for i in range(30, 40, 2)]


def test_many_open_and_few_finished_prunes_nothing():
    todos = _items(149, done=range(3))
    assert prune(todos) == (todos, [])


def test_archive_text_appends_under_one_heading_per_day():
    first = archive_text("", [{"done": True, "text": "one"}], "2026-10-01")
    same_day = archive_text(first, [{"done": True, "text": "two"}], "2026-10-01")
    next_day = archive_text(same_day, [{"done": True, "text": "three"}], "2026-10-02")
    assert first.startswith("# Todo archive\n")
    assert same_day.count("## archived") == 1 and next_day.count("## archived") == 2
    assert next_day.index("- [x] two") < next_day.index("## archived 2026-10-02") < next_day.index("- [x] three")
    assert [t["text"] for t in parse_todo_text(next_day)] == ["one", "two", "three"]


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


def _seed(n, done=()):
    """A list of n items written straight to the file: checking items off one call
    at a time on a long list would already be archiving them."""
    (_dir() / ".todo.md").write_text(render_todos(_items(n, done)))


async def test_a_call_on_a_long_list_archives_the_old_finished_items(client):
    _seed(PRUNE_ABOVE + 10, done=range(12))
    assert not (_dir() / ".todo-archive.md").exists(), "nothing is archived until a call is made"
    out = await _todo(action="add", text="one more")
    assert f"{12 - DONE_KEEP} finished item(s) moved to .todo-archive.md" in out
    archived = parse_todo_text((_dir() / ".todo-archive.md").read_text())
    assert [t["text"] for t in archived] == [f"task-{i:03d}" for i in range(12 - DONE_KEEP)]
    kept = parse_todo_text((_dir() / ".todo.md").read_text())
    assert len(kept) == PRUNE_ABOVE + 10 + 1 - (12 - DONE_KEEP)
    assert sum(1 for t in kept if t["done"]) == DONE_KEEP
    assert "task-000" not in (_dir() / ".todo.md").read_text()
    # a change after the move still lands on the right item, and the notice is not repeated
    out = await _todo(action="check", text="task-012")
    assert out.startswith("checked") and "moved to" not in out


async def test_the_archive_grows_by_appending(client):
    _seed(PRUNE_ABOVE + 10, done=range(8))
    await _todo(action="add", text="trigger")
    for i in range(8, 16):
        await _todo(action="check", text=f"task-{i:03d}")     # each call sheds the oldest finished one
    text = (_dir() / ".todo-archive.md").read_text()
    assert text.count("# Todo archive") == 1
    # a call sheds before it acts, so the last check still shows: DONE_KEEP + 1 finished
    assert [t["text"] for t in parse_todo_text(text)] == [f"task-{i:03d}" for i in range(10)]
    kept = parse_todo_text((_dir() / ".todo.md").read_text())
    assert [t["text"] for t in kept if t["done"]] == [f"task-{i:03d}" for i in range(10, 16)]


async def test_a_list_shows_the_first_open_items_and_counts_the_rest(client):
    _seed(100, done=range(3))
    out = await _todo(action="list")
    lines = out.splitlines()
    assert sum("[ ]" in l for l in lines) == OPEN_VIEW_CAP
    assert sum("[x]" in l for l in lines) == 3
    assert f"{100 - 3 - OPEN_VIEW_CAP} more open item(s) not shown" in lines[-1]
    assert "100 items, 3 done" in lines[-1]
    # positions are the real ones, and a hidden item can still be checked off by text
    assert lines[0].startswith("0. [x] task-000")
    assert "task-099" not in out
    assert (await _todo(action="check", text="task-099")).startswith("checked")


async def test_listing_a_long_list_with_old_finished_items_archives_them(client):
    _seed(PRUNE_ABOVE + 5, done=range(9))
    out = await _todo(action="list")
    assert "task-000" not in out.split("\n(")[0]
    assert "4 finished item(s) moved to" in out
    assert (_dir() / ".todo-archive.md").exists()


async def test_the_149_item_list_from_the_benchmark_run(client):
    _seed(149, done=range(15))
    out = await _todo(action="add", text="late")
    assert "10 finished item(s) moved" in out
    kept = (_dir() / ".todo.md").read_text()
    assert len(kept) < 149 * 40 and kept.count("- [x]") == DONE_KEEP and kept.count("- [ ]") == 135


async def test_a_short_list_is_unchanged(client):
    _seed(10, done=range(4))
    out = await _todo(action="list")
    assert "more open item" not in out and out.count("[x]") == 4 and out.count("[ ]") == 6
    assert not (_dir() / ".todo-archive.md").exists()
