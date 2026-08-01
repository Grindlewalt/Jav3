"""Talk-while-working: the conversation clone, the worker cap, and result
delivery. Same no-model harness as test_voice_session."""
import asyncio

import pytest

from backend.auth import hash_password
from backend.db import get_db, init_db, open_conversation
from backend.memory import ensure_memory_seeds

from tests.test_voice_session import FakeLink, make_session, settle, first_tts


@pytest.fixture
async def seeded(tmp_env):
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


# ---- the clone SQL -------------------------------------------------------------

async def test_clone_reproduces_model_facing_history(seeded):
    from backend import compaction
    from backend.voice import TWIN_ACK, clone_conversation

    db = await get_db()
    try:
        cid = await open_conversation(db, project=None, title="long chat")
        # six exchanges; pretend the first four were compacted away
        ids = []
        for i in range(6):
            cur = await db.execute(
                "INSERT INTO messages (conversation_id, role, content) "
                "VALUES (?, ?, ?)",
                (cid, "user" if i % 2 == 0 else "assistant", f"msg {i}"))
            ids.append(cur.lastrowid)
        await db.execute(
            "INSERT INTO tool_calls (conversation_id, tool, args, result) "
            "VALUES (?, 'web_search', '{}', 'r')", (cid,))
        await db.execute(
            "UPDATE conversations SET compact_summary = 'earlier: stuff', "
            "compact_upto = ? WHERE id = ?", (ids[3], cid))
        await db.commit()

        new_cid = await clone_conversation(db, cid, task="counting things",
                                           spoken="I got as far as")

        async with db.execute(
            "SELECT kind, parent_conversation_id, compact_summary, "
            "compact_upto FROM conversations WHERE id = ?", (new_cid,)) as cur:
            row = await cur.fetchone()
        assert row["kind"] == "chat"                 # shows in the sidebar
        assert row["parent_conversation_id"] == cid
        assert row["compact_summary"] == "earlier: stuff"
        assert row["compact_upto"] == 0

        # identical model-facing history + the twin-note pair on the end
        psum, prows = await compaction.load_history(db, cid)
        csum, crows = await compaction.load_history(db, new_cid)
        assert csum == psum
        ctexts = [(r["role"], r["content"]) for r in crows]
        assert ctexts[:len(prows)] == [(r["role"], r["content"]) for r in prows]
        assert ctexts[len(prows)][0] == "user"
        assert "counting things" in ctexts[len(prows)][1]
        assert "I got as far as" in ctexts[len(prows)][1]
        assert ctexts[len(prows) + 1] == ("assistant", TWIN_ACK)

        async with db.execute(
            "SELECT COUNT(*) AS c FROM tool_calls WHERE conversation_id = ?",
            (new_cid,)) as cur:
            assert (await cur.fetchone())["c"] == 1
    finally:
        await db.close()


# ---- the session flow ------------------------------------------------------------

def two_phase_turn(release: asyncio.Event):
    """Call 1: runs a tool, blocks, then finishes (the background worker).
    Later calls: answer immediately (the talking clone)."""
    calls = {"n": 0}

    async def turn(cid, system_prompt, history, tools=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"type": "tool", "id": "t1", "name": "run_code", "args": {}}
            await release.wait()
            yield {"type": "tool_result", "id": "t1", "name": "run_code",
                   "ok": True, "result": "…"}
            yield {"type": "final",
                   "content": "Finished: the count came to 1,207 files."}
        else:
            yield {"type": "token", "text": "Sure, it's about three thirty. "}
            yield {"type": "final", "content": "Sure, it's about three thirty. "}
    return turn


async def ack_all(session):
    tts = [m for m in session.link.sent_json if m["type"] == "tts"]
    for t in tts:
        await session._on_sidecar_json(
            {"type": "tts_done", "id": t["id"], "dur_ms": 500})
        await session.on_browser_json(
            {"type": "chunk_played", "chunk_id": t["id"]})


async def test_talk_while_working_clones_and_delivers(seeded, monkeypatch):
    from backend import chat as chat_mod
    from backend.voice import DELIVERY_ACK, TWIN_ACK
    session, out = make_session(monkeypatch)
    release = asyncio.Event()
    monkeypatch.setattr(chat_mod, "run_turn", two_phase_turn(release))

    await session._on_transcript("count every file in the project")
    for _ in range(300):
        await asyncio.sleep(0.01)
        if session.turn_saw_tool:
            break
    assert session.turn_saw_tool
    old_cid = session.cid

    # talk while it works → clone takes over, worker registered
    await session._on_transcript("what time is it")
    assert session.cid != old_cid
    assert old_cid in session.workers
    assert any(isinstance(m, dict) and m.get("type") == "conversation"
               and m.get("reason") == "cloned" for m in out)

    # the clone's transcript: history + twin note + new exchange
    db = await get_db()
    try:
        async with db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "ORDER BY id", (session.cid,)) as cur:
            rows = [(r["role"], r["content"]) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert rows[0] == ("user", "count every file in the project")  # copied
    twin_i = next(i for i, r in enumerate(rows)
                  if r[0] == "user" and "your twin" in r[1].lower())
    assert rows[twin_i + 1] == ("assistant", TWIN_ACK)
    assert ("user", "what time is it") in rows

    # the clone answers while the worker still runs
    await settle(session)
    assert not session.workers[old_cid]["watcher"].done()
    await ack_all(session)
    assert session.state == "listening"

    # worker finishes → durable delivery rows + spoken digest
    watcher = session.workers[old_cid]["watcher"]
    release.set()
    await asyncio.wait_for(watcher, 5)
    assert old_cid not in session.workers

    db = await get_db()
    try:
        async with db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "ORDER BY id", (session.cid,)) as cur:
            rows = [(r["role"], r["content"]) for r in await cur.fetchall()]
    finally:
        await db.close()
    deliver_i = next(i for i, r in enumerate(rows)
                     if r[0] == "user" and "background result" in r[1])
    assert "1,207 files" in rows[deliver_i][1]
    assert rows[deliver_i + 1] == ("assistant", DELIVERY_ACK)

    spoken = [m for m in session.link.sent_json if m["type"] == "tts"
              and "Done with the earlier task" in m["text"]]
    assert spoken, "the delivery digest should be spoken at idle"


async def test_cap_queues_instead_of_fourth_clone(seeded, monkeypatch):
    from backend import chat as chat_mod, voice
    session, out = make_session(monkeypatch)
    release = asyncio.Event()
    monkeypatch.setattr(chat_mod, "run_turn", two_phase_turn(release))
    # three phantom workers occupy the cap
    for fake_cid in (901, 902, 903):
        session.workers[fake_cid] = {"task": "…", "watcher": None,
                                     "status": "working"}

    await session._on_transcript("do the big thing")
    for _ in range(300):
        await asyncio.sleep(0.01)
        if session.turn_saw_tool:
            break
    old_cid = session.cid

    await session._on_transcript("and one more thing")
    assert session.cid == old_cid                 # no fourth clone
    assert session.queued == ["and one more thing"]
    cap = [m for m in session.link.sent_json
           if m["type"] == "tts" and m["text"] == voice.CAP_LINE]
    assert cap, "the at-capacity line should be spoken"

    release.set()
    await settle(session)
    await ack_all(session)
    # the parked utterance drained into its own turn once the talker freed up
    db = await get_db()
    try:
        async with db.execute(
            "SELECT content FROM messages WHERE conversation_id = ? AND "
            "role = 'user' ORDER BY id", (old_cid,)) as cur:
            users = [r["content"] for r in await cur.fetchall()]
    finally:
        await db.close()
    assert users == ["do the big thing", "and one more thing"]
