"""A turn that died mid-way can be resumed.

The failure path leaves an assistant row "(turn failed: ...)" with the turn's
tool calls linked to it, but the model-facing history is prose only, so a
"continue" after the death met one failure line and none of the steps. Now the
turn that died directly before a new message keeps its tool work in the history
(compaction._with_failed_turn), and POST /api/chat/{id}/resume sends the fixed
"continue" message.

httpx's ASGITransport buffers a streaming response until the app finishes, so
a live turn is driven as asyncio tasks (see test_chat_robustness.py)."""
import asyncio
import contextlib
import json

import httpx
import pytest

from backend import chat as chat_mod
from backend import compaction
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db, open_conversation
from backend.main import app
from backend.memory import ensure_memory_seeds


# --- history assembly --------------------------------------------------------


@pytest.fixture
async def conv(tmp_env):
    await init_db()
    db = await get_db()
    try:
        cid = await open_conversation(db, project=None, title="t")
        yield cid, db
    finally:
        await db.close()


async def _user(db, cid, text):
    cur = await db.execute("INSERT INTO messages (conversation_id, role, content) "
                           "VALUES (?, 'user', ?)", (cid, text))
    await db.commit()
    return cur.lastrowid


async def _turn(db, cid, reply, calls=()):
    """One persisted assistant row with its tool calls linked, as chat.py writes
    it (a finished reply and a failure row are written the same way)."""
    async with db.execute("SELECT COALESCE(MAX(id), 0) m FROM tool_calls "
                          "WHERE conversation_id = ?", (cid,)) as cur:
        before = (await cur.fetchone())["m"]
    for tool, args, result in calls:
        await db.execute(
            "INSERT INTO tool_calls (conversation_id, tool, args, result) "
            "VALUES (?, ?, ?, ?)",
            (cid, tool, args if isinstance(args, str) else json.dumps(args), result))
    cur = await db.execute("INSERT INTO messages (conversation_id, role, content) "
                           "VALUES (?, 'assistant', ?)", (cid, reply))
    await chat_mod._link_tool_calls(db, cid, before, cur.lastrowid)
    await db.commit()
    return cur.lastrowid


DIED = "(turn failed: guest closed the connection mid-turn)"


async def _died(db, cid, calls, reply=DIED, ask="build the game"):
    await _user(db, cid, ask)
    return await _turn(db, cid, reply, calls)


def _is_note(m):
    return m["role"] == "user" and m["content"].startswith(
        "[The previous turn died before it finished")


def test_is_failed_turn_knows_both_forms():
    assert compaction.is_failed_turn("(turn failed: guest closed the connection)")
    assert compaction.is_failed_turn("  (guest loop error: ModelError: boom)")
    assert not compaction.is_failed_turn("Done. The turn failed tests pass now.")
    assert not compaction.is_failed_turn("[Request interrupted by operator]")
    assert not compaction.is_failed_turn(None)
    assert not compaction.is_failed_turn("")


async def test_a_dead_turn_before_the_new_message_replays_its_steps(conv):
    cid, db = conv
    await _died(db, cid, [("run_code", {"command": "npm test"}, "3 passed"),
                          ("write_file", {"path": "a.txt", "content": "hi"}, "wrote a.txt")])
    await _user(db, cid, "alr sorry you may continue")

    history = await compaction.assemble(db, cid, "sys", failed_trace=600)

    assert [m["role"] for m in history] == [
        "user", "assistant", "tool", "assistant", "tool", "assistant", "user", "user"]
    assert history[0]["content"] == "build the game"
    first, answer, second = history[1], history[2], history[3]
    assert first["tool_calls"][0]["function"]["name"] == "run_code"
    assert json.loads(first["tool_calls"][0]["function"]["arguments"]) == {"command": "npm test"}
    assert answer["content"] == "3 passed"
    assert answer["tool_call_id"] == first["tool_calls"][0]["id"]
    assert second["tool_calls"][0]["function"]["name"] == "write_file"
    assert history[5] == {"role": "assistant", "content": DIED}
    # the note rides ahead of the new message, so the new message wins
    assert _is_note(history[6])
    assert history[7] == {"role": "user", "content": "alr sorry you may continue"}
    note = history[6]["content"]
    assert "guest closed the connection mid-turn" in note
    assert "last 2 tool calls are in the history above" in note
    assert "may be gone" in note and "checking the current state" in note
    assert "answer this message on its own" in note


async def test_results_and_long_arguments_are_trimmed_but_stay_valid_json(conv):
    cid, db = conv
    await _died(db, cid, [("write_file", {"path": "big.js", "content": "x" * 5000},
                           "y" * 5000)])
    await _user(db, cid, "continue")

    history = await compaction.assemble(db, cid, "sys", failed_trace=600)

    call, result = history[1]["tool_calls"][0]["function"], history[2]["content"]
    assert len(result) < 700 and result.startswith("y" * 600) and "[+4400 chars]" in result
    args = json.loads(call["arguments"])                  # still parses
    assert args["path"] == "big.js"
    assert len(args["content"]) < 700 and "[+4400 chars]" in args["content"]


async def test_unparseable_arguments_are_cut_into_valid_json(conv):
    cid, db = conv
    await _died(db, cid, [("run_code", '{"command": "' + "z" * 3000, "ok")])
    await _user(db, cid, "continue")

    history = await compaction.assemble(db, cid, "sys", failed_trace=600)

    args = json.loads(history[1]["tool_calls"][0]["function"]["arguments"])
    assert args["arguments"].startswith('{"command": "zzz') and "chars]" in args["arguments"]


async def test_a_300_call_turn_keeps_only_its_last_calls(conv):
    cid, db = conv
    n = compaction.FAILED_TURN_TRACE_CALLS
    await _died(db, cid, [("run_code", {"command": f"step {i}"}, f"out {i}")
                          for i in range(300)])
    await _user(db, cid, "continue")

    history = await compaction.assemble(db, cid, "sys", failed_trace=600)

    calls = [m for m in history if m.get("tool_calls")]
    assert len(calls) == n
    assert json.loads(calls[0]["tool_calls"][0]["function"]["arguments"]) == \
        {"command": f"step {300 - n}"}
    assert json.loads(calls[-1]["tool_calls"][0]["function"]["arguments"]) == \
        {"command": "step 299"}
    note = next(m["content"] for m in history if _is_note(m))
    assert f"last {n} tool calls" in note and f"{300 - n} earlier ones are not shown" in note


async def test_a_dead_turn_with_no_saved_calls_still_gets_the_note(conv):
    cid, db = conv
    await _died(db, cid, [])
    await _user(db, cid, "try again")

    history = await compaction.assemble(db, cid, "sys", failed_trace=600)

    assert [m["role"] for m in history] == ["user", "assistant", "user", "user"]
    assert "saved no tool calls" in history[2]["content"]


async def test_the_older_guest_loop_error_form_is_replayed_too(conv):
    cid, db = conv
    await _died(db, cid, [("run_code", {"command": "ls"}, "a b")],
                reply="(guest loop error: ModelError: upstream 502)")
    await _user(db, cid, "continue")

    history = await compaction.assemble(db, cid, "sys", failed_trace=600)

    assert any(m.get("tool_calls") for m in history)
    note = next(m["content"] for m in history if _is_note(m))
    assert "ModelError: upstream 502" in note and "guest loop error" not in note


async def test_a_long_failure_reason_is_cut_in_the_note(conv):
    cid, db = conv
    await _died(db, cid, [], reply=f"(turn failed: {'boom ' * 200})")
    await _user(db, cid, "continue")

    history = await compaction.assemble(db, cid, "sys", failed_trace=600)

    note = next(m["content"] for m in history if _is_note(m))
    assert len(note) < 900 and "…" in note


async def test_a_finished_turn_is_not_replayed(conv):
    cid, db = conv
    await _user(db, cid, "run the tests")
    await _turn(db, cid, "All three pass.", [("run_code", {"command": "npm test"}, "3 passed")])
    await _user(db, cid, "thanks")

    history = await compaction.assemble(db, cid, "sys", failed_trace=600)

    assert [m["role"] for m in history] == ["user", "assistant", "user"]
    assert not any("tool_calls" in m or _is_note(m) for m in history)


async def test_a_dead_turn_that_is_not_the_last_is_not_replayed(conv):
    cid, db = conv
    await _died(db, cid, [("run_code", {"command": "make"}, "ok")])
    await _user(db, cid, "what is 2+2")
    await _turn(db, cid, "4")                      # the operator moved on and it answered
    await _user(db, cid, "thanks")

    history = await compaction.assemble(db, cid, "sys", failed_trace=600)

    assert not any("tool_calls" in m or _is_note(m) for m in history)
    assert [m["content"] for m in history][-3:] == ["what is 2+2", "4", "thanks"]


async def test_only_the_most_recent_dead_turn_is_replayed(conv):
    cid, db = conv
    await _died(db, cid, [("run_code", {"command": "first attempt"}, "r1")])
    await _died(db, cid, [("run_code", {"command": "second attempt"}, "r2")], ask="retry")
    await _user(db, cid, "continue")

    history = await compaction.assemble(db, cid, "sys", failed_trace=600)

    cmds = [json.loads(m["tool_calls"][0]["function"]["arguments"])["command"]
            for m in history if m.get("tool_calls")]
    assert cmds == ["second attempt"]
    assert sum(_is_note(m) for m in history) == 1


async def test_an_operator_stop_is_not_a_failed_turn(conv):
    cid, db = conv
    await _died(db, cid, [("run_code", {"command": "x"}, "y")],
                reply=chat_mod.INTERRUPTED_MARKER)
    await _user(db, cid, "continue")

    history = await compaction.assemble(db, cid, "sys", failed_trace=600)

    assert not any("tool_calls" in m or _is_note(m) for m in history)


async def test_replay_is_off_when_the_setting_is_zero(conv):
    cid, db = conv
    await _died(db, cid, [("run_code", {"command": "x"}, "y")])
    await _user(db, cid, "continue")

    history = await compaction.assemble(db, cid, "sys", failed_trace=0)

    assert [m["role"] for m in history] == ["user", "assistant", "user"]


async def test_the_voice_tool_trace_path_is_unchanged(conv):
    cid, db = conv
    await _died(db, cid, [("run_code", {"command": "x"}, "y" * 900)])
    await _user(db, cid, "continue")

    history = await compaction.assemble(db, cid, "sys", tool_trace=200, failed_trace=600)

    assert len(history[2]["content"]) == 200               # the voice cap, not 600
    assert not any(_is_note(m) for m in history)


# --- the turn and the endpoint ----------------------------------------------


@pytest.fixture
async def client(tmp_env, monkeypatch):
    await init_db()
    ensure_memory_seeds()

    async def no_naming(*a, **k):
        return None
    monkeypatch.setattr(chat_mod, "_name_conversation", no_naming)
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


def _guest(seen, text="done", calls=0, die=None, block=None, started=None):
    """A stand-in guest turn: records the history it was handed, runs `calls`
    tool calls, then answers, or raises `die`."""
    async def turn(cid, system_prompt, history, tools=None, **kw):
        seen.append(history)
        if started is not None:
            started.set()
        if block is not None:
            await block.wait()
        for i in range(calls):
            yield {"type": "tool", "id": f"c{i}", "name": "run_code",
                   "args": {"command": f"step {i}"}}
            yield {"type": "tool_result", "id": f"c{i}", "name": "run_code",
                   "result": f"out {i}"}
        if die is not None:
            raise die
        yield {"type": "final", "content": text}
    return turn


async def _settle(cid):
    task = chat_mod._active_turns.get(cid)
    if task:
        with contextlib.suppress(BaseException):
            await task
    # the journal line a finished turn spawns holds its own connection
    await asyncio.gather(*chat_mod._background, return_exceptions=True)


async def _say(client, monkeypatch, message, guest, cid=None):
    """Post a message with `guest` standing in for the guest turn; the response
    arrives when the turn ends."""
    monkeypatch.setattr(chat_mod, "guest_turn", guest)
    body = {"message": message, **({"conversation_id": cid} if cid else {})}
    return await client.post("/api/chat", json=body)


async def _rows(client, cid):
    r = await client.get(f"/api/conversations/{cid}/messages")
    return [(m["role"], m["content"]) for m in r.json()["messages"]]


async def _dead_chat(client, monkeypatch, calls=3):
    """A chat whose first turn ran `calls` tool calls and then died."""
    seen = []
    await _say(client, monkeypatch, "build the game", _guest(
        seen, calls=calls, die=RuntimeError("guest closed the connection mid-turn")))
    r = await client.get("/api/conversations")
    cid = r.json()["conversations"][0]["id"]
    await _settle(cid)
    return cid


async def test_continue_after_a_dead_turn_shows_the_model_its_steps(client, monkeypatch):
    cid = await _dead_chat(client, monkeypatch, calls=3)
    rows = await _rows(client, cid)
    assert rows[-1][0] == "assistant" and rows[-1][1].startswith("(turn failed:")

    seen = []
    await _say(client, monkeypatch, "alr sorry you may continue", _guest(seen), cid=cid)
    await _settle(cid)

    h = seen[0]
    assert h[-1]["content"] == "alr sorry you may continue"
    assert _is_note(h[-2]) and "guest closed the connection mid-turn" in h[-2]["content"]
    assert h[-3]["content"].startswith("(turn failed:")
    replayed = [json.loads(m["tool_calls"][0]["function"]["arguments"])["command"]
                for m in h if m.get("tool_calls")]
    assert replayed == ["step 0", "step 1", "step 2"]
    assert [m["content"] for m in h if m["role"] == "tool"] == ["out 0", "out 1", "out 2"]
    # the replay is the model's view only: the saved transcript is unchanged
    assert all(role in ("user", "assistant") for role, _ in await _rows(client, cid))


async def test_a_turn_after_a_normal_answer_carries_no_replay(client, monkeypatch):
    seen = []
    await _say(client, monkeypatch, "hi", _guest(seen, calls=2, text="hello"))
    cid = (await client.get("/api/conversations")).json()["conversations"][0]["id"]
    await _settle(cid)
    seen2 = []
    await _say(client, monkeypatch, "thanks", _guest(seen2), cid=cid)
    await _settle(cid)
    assert not any("tool_calls" in m or _is_note(m) for m in seen2[0])


async def test_the_replay_follows_the_setting(client, monkeypatch):
    cid = await _dead_chat(client, monkeypatch)
    monkeypatch.setattr(settings, "failed_turn_trace_chars", 0)
    seen = []
    await _say(client, monkeypatch, "continue", _guest(seen), cid=cid)
    await _settle(cid)
    assert not any("tool_calls" in m or _is_note(m) for m in seen[0])


async def test_resume_sends_the_fixed_message_and_the_model_sees_the_steps(client, monkeypatch):
    cid = await _dead_chat(client, monkeypatch, calls=2)
    seen = []
    monkeypatch.setattr(chat_mod, "guest_turn", _guest(seen, text="picked it up"))

    # the web posts its usual chat body (and its tab id); the endpoint reads only the tab
    r = await client.post(f"/api/chat/{cid}/resume", json={
        "message": "ignored", "conversation_id": cid, "ephemeral": False, "tab": "t1"})
    await _settle(cid)

    assert r.status_code == 200
    assert chat_mod.RESUME_MESSAGE == "Continue from where the previous turn stopped."
    rows = await _rows(client, cid)
    assert rows[-2] == ("user", chat_mod.RESUME_MESSAGE)
    assert rows[-1] == ("assistant", "picked it up")
    h = seen[0]
    assert h[-1]["content"] == chat_mod.RESUME_MESSAGE
    assert _is_note(h[-2])
    assert len([m for m in h if m.get("tool_calls")]) == 2
    assert not chat_mod._posting


async def test_resume_is_refused_once_the_chat_has_moved_on(client, monkeypatch):
    cid = await _dead_chat(client, monkeypatch)
    await _say(client, monkeypatch, "what is 2+2", _guest([], text="4"), cid=cid)
    await _settle(cid)

    r = await client.post(f"/api/chat/{cid}/resume")

    assert r.status_code == 409
    assert "nothing to resume" in r.json()["detail"]
    assert not chat_mod._posting


async def test_resume_is_refused_after_a_normal_answer(client, monkeypatch):
    await _say(client, monkeypatch, "hi", _guest([], text="hello"))
    cid = (await client.get("/api/conversations")).json()["conversations"][0]["id"]
    await _settle(cid)

    r = await client.post(f"/api/chat/{cid}/resume")

    assert r.status_code == 409 and "nothing to resume" in r.json()["detail"]


async def test_resume_is_refused_while_a_turn_runs(client, monkeypatch):
    cid = await _dead_chat(client, monkeypatch)
    release, started = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(chat_mod, "guest_turn",
                        _guest([], block=release, started=started))
    post = asyncio.create_task(client.post(
        "/api/chat", json={"message": "again", "conversation_id": cid}))
    await asyncio.wait_for(started.wait(), 5)

    r = await client.post(f"/api/chat/{cid}/resume")

    assert r.status_code == 409 and r.json()["detail"] == "turn_in_progress"
    release.set()
    await asyncio.wait_for(post, 5)
    await _settle(cid)


async def test_resume_of_an_unknown_chat_is_a_404(client):
    r = await client.post("/api/chat/9999/resume")
    assert r.status_code == 404


async def test_a_second_resume_while_the_first_runs_is_refused(client, monkeypatch):
    cid = await _dead_chat(client, monkeypatch)
    release, started = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(chat_mod, "guest_turn",
                        _guest([], block=release, started=started))
    first = asyncio.create_task(client.post(f"/api/chat/{cid}/resume"))
    await asyncio.wait_for(started.wait(), 5)

    r = await client.post(f"/api/chat/{cid}/resume")

    assert r.status_code == 409 and r.json()["detail"] == "turn_in_progress"
    release.set()
    await asyncio.wait_for(first, 5)
    await _settle(cid)
