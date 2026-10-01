"""The agent's text between its tool calls is kept after the turn: stored in
order beside the calls it sat between (backend/narration.py), served by
GET /api/conversations/{id}/messages (`narration`, `pending_narration`) and by
the Logs transcript, wiped with the chat, never written for an incognito turn,
and absent (not an error) for a conversation from before it was kept."""
import asyncio

import httpx
import pytest

from backend import narration
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
            ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


def _turn(*script):
    """A guest_turn stand-in that yields `script` (events) and ends."""
    async def turn(cid, system_prompt, history, tools=None, **kw):
        for ev in script:
            yield ev
    return turn


def _tok(text):
    return {"type": "token", "text": text}


def _call(i, name="read_file", **args):
    return [{"type": "tool", "id": f"c{i}", "name": name, "args": args},
            {"type": "tool_result", "id": f"c{i}", "name": name, "ok": True,
             "result": f"result {i}"}]


TWO_ROUNDS = [
    _tok("Let me look "), _tok("at the config."),
    *_call(1, path="a.py"),
    _tok("Now the tests."),
    *_call(2, path="b.py"), *_call(3, path="c.py"),
    _tok("All done, "), _tok("here is the answer."),
    {"type": "final", "content": "All done, here is the answer."},
]


async def _rows(sql, *args):
    db = await get_db()
    try:
        async with db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _post(client, monkeypatch, script, **body):
    from backend import chat as chat_mod
    monkeypatch.setattr(chat_mod, "guest_turn", _turn(*script))
    r = await client.post("/api/chat", json={"message": "hi", **body})
    assert r.status_code == 200
    ids = await _rows("SELECT id FROM conversations ORDER BY id DESC LIMIT 1")
    return ids[0]["id"] if ids else None


# ---- the recorder -------------------------------------------------------------

async def _record(events, *, enabled=True, cid=None):
    """Run a Recorder over `events` the way a driver does: a tool_calls row
    lands at each tool_result, before the next round's tool event."""
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (title) VALUES ('t')")
        cid = cur.lastrowid
        rec = narration.Recorder(db, cid, enabled=enabled)
        for ev in events:
            await rec.feed(ev)
            if ev["type"] == "tool_result":
                await db.execute(
                    "INSERT INTO tool_calls (conversation_id, tool, args, result) "
                    "VALUES (?, ?, '{}', ?)", (cid, ev["name"], ev["result"]))
                await db.commit()
        cur = await db.execute(
            "INSERT INTO messages (conversation_id, role, content) "
            "VALUES (?, 'assistant', 'x')", (cid,))
        await rec.link(cur.lastrowid)
        return cid, cur.lastrowid
    finally:
        await db.close()


async def test_recorder_stores_text_between_calls_not_the_reply(tmp_env):
    await init_db()
    cid, mid = await _record(TWO_ROUNDS)
    rows = await _rows("SELECT * FROM turn_narration WHERE conversation_id=? "
                       "ORDER BY id", cid)
    assert [r["text"] for r in rows] == ["Let me look at the config.",
                                         "Now the tests."]
    calls = await _rows("SELECT id FROM tool_calls WHERE conversation_id=? "
                        "ORDER BY id", cid)
    # opens the turn (after no call), then follows the first call
    assert [r["after_call_id"] for r in rows] == [0, calls[0]["id"]]
    assert {r["message_id"] for r in rows} == {mid}


async def test_recorder_drops_text_a_retried_stream_had_streamed(tmp_env):
    """M2: a model stream that drops mid-round is re-asked (a `retry` event);
    the partial text must not be stored next to the full one."""
    await init_db()
    cid, _ = await _record([_tok("half a tho"), {"type": "retry"},
                            _tok("Whole thought."), *_call(1)])
    rows = await _rows("SELECT text FROM turn_narration WHERE conversation_id=?", cid)
    assert [r["text"] for r in rows] == ["Whole thought."]


async def test_recorder_skips_blank_text_and_parallel_calls(tmp_env):
    await init_db()
    # a round of two parallel calls yields two tool events but one stretch of
    # text; whitespace alone is nothing
    events = [_tok("  \n"), *_call(1), _tok("go"),
              {"type": "tool", "id": "a", "name": "t", "args": {}},
              {"type": "tool", "id": "b", "name": "t", "args": {}},
              {"type": "tool_result", "id": "a", "name": "t", "ok": True, "result": "1"},
              {"type": "tool_result", "id": "b", "name": "t", "ok": True, "result": "2"},
              {"type": "final", "content": "ok"}]
    cid, _ = await _record(events)
    rows = await _rows("SELECT text FROM turn_narration WHERE conversation_id=?", cid)
    assert [r["text"] for r in rows] == ["go"]


async def test_recorder_disabled_writes_nothing(tmp_env):
    await init_db()
    await _record(TWO_ROUNDS, enabled=False)
    assert await _rows("SELECT * FROM turn_narration") == []


async def test_recorder_clips_a_runaway_segment(tmp_env):
    await init_db()
    cid, _ = await _record([_tok("x" * (narration.MAX_SEGMENT_CHARS + 500)),
                            *_call(1), {"type": "final", "content": "."}])
    rows = await _rows("SELECT text FROM turn_narration WHERE conversation_id=?", cid)
    assert len(rows[0]["text"]) == narration.MAX_SEGMENT_CHARS


async def test_migration_is_idempotent(tmp_env):
    await init_db()
    await init_db()
    cols = await _rows("PRAGMA table_info(turn_narration)")
    assert {"conversation_id", "message_id", "after_call_id", "text"} <= {
        c["name"] for c in cols}


# ---- the pure placement -------------------------------------------------------

def test_for_message_counts_the_calls_before_each_text():
    rows = [{"after_call_id": 0, "text": "a"}, {"after_call_id": 5, "text": "b"},
            {"after_call_id": 5, "text": "c"}, {"after_call_id": 7, "text": "d"}]
    # this reply's calls are 5, 6, 7 (an earlier turn's calls have lower ids)
    assert narration.for_message([5, 6, 7], rows) == [
        {"before": 0, "text": "a"}, {"before": 1, "text": "b"},
        {"before": 1, "text": "c"}, {"before": 3, "text": "d"}]


def test_merge_timeline_places_text_before_the_next_call():
    items = [{"kind": "message", "id": 1, "role": "user", "ts": "t1"},
             {"kind": "tool", "id": 10, "ts": "t1"},
             {"kind": "tool", "id": 11, "ts": "t1"},
             {"kind": "message", "id": 2, "role": "assistant", "ts": "t1"}]
    rows = [{"id": 1, "message_id": 2, "after_call_id": 9, "text": "first", "ts": "t1"},
            {"id": 2, "message_id": 2, "after_call_id": 10, "text": "second", "ts": "t1"}]
    out = narration.merge_timeline(items, rows)
    assert [(i["kind"], i.get("text") or i["id"]) for i in out] == [
        ("message", 1), ("narration", "first"), ("tool", 10),
        ("narration", "second"), ("tool", 11), ("message", 2)]


def test_merge_timeline_never_slides_past_its_own_reply():
    # an interrupted turn's text: its call never ran, so the next call in the
    # table belongs to a LATER turn. The text stays before its own reply.
    items = [{"kind": "message", "id": 1, "role": "user", "ts": "t1"},
             {"kind": "message", "id": 2, "role": "assistant", "ts": "t1"},
             {"kind": "message", "id": 3, "role": "user", "ts": "t2"},
             {"kind": "tool", "id": 50, "ts": "t2"},
             {"kind": "message", "id": 4, "role": "assistant", "ts": "t2"}]
    rows = [{"id": 1, "message_id": 2, "after_call_id": 40, "text": "cut off", "ts": "t1"}]
    out = narration.merge_timeline(items, rows)
    assert [i["kind"] for i in out].index("narration") == 1


# ---- the API ------------------------------------------------------------------

async def test_messages_api_returns_narration_in_order(client, monkeypatch):
    cid = await _post(client, monkeypatch, TWO_ROUNDS)
    body = (await client.get(f"/api/conversations/{cid}/messages")).json()
    reply = [m for m in body["messages"] if m["role"] == "assistant"][-1]
    assert reply["content"] == "All done, here is the answer."
    assert [a["name"] for a in reply["activity"]] == ["read_file"] * 3
    # `activity` stays tool-only; the text rides beside it with its position
    assert reply["narration"] == [
        {"before": 0, "text": "Let me look at the config."},
        {"before": 1, "text": "Now the tests."}]
    assert body["pending_narration"] == []


async def test_a_conversation_from_before_has_no_narration(client, monkeypatch):
    cid = await _post(client, monkeypatch, TWO_ROUNDS)
    db = await get_db()
    try:
        await db.execute("DELETE FROM turn_narration")
        await db.commit()
    finally:
        await db.close()
    body = (await client.get(f"/api/conversations/{cid}/messages")).json()
    reply = [m for m in body["messages"] if m["role"] == "assistant"][-1]
    assert "narration" not in reply
    assert len(reply["activity"]) == 3          # the rest reads as it always did


async def test_second_turn_narration_binds_to_its_own_reply(client, monkeypatch):
    cid = await _post(client, monkeypatch, TWO_ROUNDS)
    from backend import chat as chat_mod
    monkeypatch.setattr(chat_mod, "guest_turn", _turn(
        _tok("Checking one thing."), *_call(9, path="z.py"),
        {"type": "final", "content": "Second answer."}))
    r = await client.post("/api/chat", json={"message": "again",
                                             "conversation_id": cid})
    assert r.status_code == 200
    replies = [m for m in (await client.get(
        f"/api/conversations/{cid}/messages")).json()["messages"]
        if m["role"] == "assistant"]
    assert len(replies) == 2
    assert len(replies[0]["narration"]) == 2
    assert replies[1]["narration"] == [{"before": 0, "text": "Checking one thing."}]


async def test_incognito_turn_stores_no_narration(client, monkeypatch):
    await _post(client, monkeypatch, TWO_ROUNDS, ephemeral=True)
    assert await _rows("SELECT * FROM turn_narration") == []
    assert await _rows("SELECT * FROM conversations") == []


async def test_deleting_a_chat_clears_its_narration(client, monkeypatch):
    cid = await _post(client, monkeypatch, TWO_ROUNDS)
    assert len(await _rows("SELECT * FROM turn_narration")) == 2
    r = await client.delete(f"/api/conversations/{cid}")
    assert r.status_code == 200
    assert await _rows("SELECT * FROM turn_narration") == []


async def test_running_turn_reports_the_text_so_far(client, monkeypatch):
    from backend import chat as chat_mod
    release, started = asyncio.Event(), asyncio.Event()

    async def turn(cid, system_prompt, history, tools=None, **kw):
        yield _tok("First I read it.")
        for ev in _call(1, path="a.py"):
            yield ev
        yield _tok("Then I change it.")
        yield {"type": "tool", "id": "c2", "name": "edit_file", "args": {}}
        started.set()
        await release.wait()
        yield {"type": "tool_result", "id": "c2", "name": "edit_file", "ok": True,
               "result": "done"}
        yield {"type": "final", "content": "fin"}

    monkeypatch.setattr(chat_mod, "guest_turn", turn)
    post = asyncio.create_task(client.post("/api/chat", json={"message": "go"}))
    await asyncio.wait_for(started.wait(), 5)
    cid = max(chat_mod._active_turns)
    body = (await client.get(f"/api/conversations/{cid}/messages")).json()
    assert body["running"] is True
    assert [a["name"] for a in body["pending_activity"]] == ["read_file"]
    assert body["pending_narration"] == [
        {"before": 0, "text": "First I read it."},
        {"before": 1, "text": "Then I change it."}]
    release.set()
    await asyncio.wait_for(post, 5)


async def test_logs_transcript_interleaves_narration(client, monkeypatch):
    cid = await _post(client, monkeypatch, TWO_ROUNDS)
    t = (await client.get(f"/api/logs/conversations/{cid}")).json()["timeline"]
    shape = [(i["kind"], i.get("role") or i.get("tool")) for i in t]
    assert shape == [
        ("message", "user"),
        ("narration", None), ("tool", "read_file"),
        ("narration", None), ("tool", "read_file"), ("tool", "read_file"),
        ("message", "assistant")]
    assert [i["text"] for i in t if i["kind"] == "narration"] == [
        "Let me look at the config.", "Now the tests."]
