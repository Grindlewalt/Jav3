"""model_calls.created_at and conversations.started_at: one clock (UTC, SQLite's
own `datetime('now')`), one format, and a usage row never outlives its chat under
the chat's id.

Benchmark-game run: model_calls for heads 653 and 665 and chats 575 and 576 were
stamped 8 to 28 hours before their conversations started. It was not a second
clock (every writer stamps UTC by the column default): the earlier owners of those
ids had been deleted, SQLite reused the ids, and the old calls came along."""
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.agent import model as model_mod
from backend.db import get_db, init_db, open_conversation

STAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")


async def _q(sql, args=()):
    db = await get_db()
    try:
        async with db.execute(sql, args) as cur:
            return [tuple(r) for r in await cur.fetchall()]
    finally:
        await db.close()


def _as_utc(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


@pytest.fixture
def far_timezone(monkeypatch):
    """A process clock a long way from UTC: a writer that used local time would be
    13 hours out here."""
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


async def test_both_stamps_are_utc_in_one_format_whatever_the_process_clock(tmp_env, far_timezone):
    await init_db()
    assert abs((datetime.now() - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()) > 3600, \
        "the fixture did not move the local clock"
    db = await get_db()
    try:
        cid = await open_conversation(db, project=None, title="t")
    finally:
        await db.close()
    await model_mod.record_model_call(
        cid, "m", {"prompt_tokens": 3, "completion_tokens": 1}, [{"role": "user", "content": "hi"}], None)
    [(started,)] = await _q("SELECT started_at FROM conversations WHERE id = ?", (cid,))
    [(created,)] = await _q("SELECT created_at FROM model_calls WHERE conversation_id = ?", (cid,))
    now = datetime.now(timezone.utc)
    for stamp in (started, created):
        assert STAMP.match(stamp), stamp
        assert abs((now - _as_utc(stamp)).total_seconds()) < 30, f"{stamp} is not UTC now ({now})"
    assert _as_utc(created) >= _as_utc(started)


def test_no_writer_stamps_either_column_itself():
    """Every insert leaves the columns to their UTC default, and nothing asks SQLite
    for local time: that is how a second clock would get in."""
    root = Path(__file__).resolve().parents[1] / "backend"
    inserts = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(model_calls|conversations)\s*\(([^)]*)\)", re.I)
    updates = re.compile(r"UPDATE\s+(model_calls|conversations)\s+SET\s+[^\"']*?\b(created_at|started_at)\s*=", re.I)
    seen = 0
    for path in root.rglob("*.py"):
        text = path.read_text()
        assert not re.search(r"""['"]localtime['"]""", text), f"{path.name}: SQLite local time"
        for m in inserts.finditer(text):
            seen += 1
            assert not re.search(r"\b(created_at|started_at)\b", m.group(2)), \
                f"{path.name}: INSERT INTO {m.group(1)} sets its own timestamp"
        assert not updates.search(text), f"{path.name}: rewrites a started_at/created_at"
    assert seen >= 2, "the scan found no inserts: its pattern is stale"


async def test_deleting_a_conversation_by_any_path_detaches_its_usage(tmp_env):
    """The incognito wipe deletes rows with plain SQL, not chat.py's delete route."""
    await init_db()
    db = await get_db()
    try:
        cid = await open_conversation(db, project=None, title="old")
        await db.execute("INSERT INTO model_calls (conversation_id, model, created_at) "
                         "VALUES (?, 'm', '2026-09-29 21:06:22')", (cid,))
        await db.execute("INSERT INTO turn_stats (conversation_id) VALUES (?)", (cid,))
        await db.execute("DELETE FROM conversations WHERE id = ?", (cid,))
        await db.commit()
        again = await open_conversation(db, project=None, title="new")
    finally:
        await db.close()
    assert again == cid, "the id is reused, which is why the usage must not keep it"
    assert await _q("SELECT conversation_id FROM model_calls") == [(None,)]
    assert await _q("SELECT conversation_id FROM turn_stats") == [(None,)]
    assert await _q("SELECT COUNT(*) FROM model_calls WHERE conversation_id = ?", (again,)) == [(0,)]


async def test_init_db_twice_keeps_the_one_trigger(tmp_env):
    await init_db()
    await init_db()
    assert await _q("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                    "AND name='conversations_detach_usage'") == [(1,)]
