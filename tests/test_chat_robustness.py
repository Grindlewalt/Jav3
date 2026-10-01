"""Chat turn robustness (M1, second hunt): the double-submit claim, failure
text and transcript, provider balance, publish-before-journal, guarded
cleanup, incognito visibility, SSE keepalive, delete-while-running and the
interrupted-request note.

httpx's ASGITransport buffers a streaming response until the app finishes, so
a live turn is driven as asyncio tasks (see test_background_chat.py)."""
import asyncio
import contextlib
import logging

import httpx
import pytest

from backend import chat as chat_mod
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


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


def _blocked(release: asyncio.Event, started: asyncio.Event, text="done", seen=None):
    async def turn(cid, system_prompt, history, tools=None, **kw):
        if seen is not None:
            seen.append({"cid": cid, "history": history})
        started.set()
        await release.wait()
        yield {"type": "final", "content": text}
    return turn


def _raising(exc):
    async def turn(cid, system_prompt, history, tools=None, **kw):
        yield {"type": "token", "text": "working on it"}
        raise exc
    return turn


async def _settle(cid: int):
    task = chat_mod._active_turns.get(cid)
    if task:
        with contextlib.suppress(BaseException):
            await task


async def _first_conversation(client, monkeypatch, **body) -> int:
    """One finished turn, so a conversation exists; returns its id."""
    monkeypatch.setattr(chat_mod, "guest_turn", _blocked(
        done := asyncio.Event(), started := asyncio.Event(), "first"))
    post = asyncio.create_task(client.post("/api/chat", json={"message": "hi", **body}))
    await asyncio.wait_for(started.wait(), 5)
    cid = max(chat_mod._active_turns)
    done.set()
    await asyncio.wait_for(post, 5)
    await _settle(cid)
    return cid


async def _roles(client, cid):
    r = await client.get(f"/api/conversations/{cid}/messages")
    return [(m["role"], m["content"]) for m in r.json()["messages"]]


# --- ROBUST-07: the 409 guard is one synchronous claim ----------------------


async def test_double_submit_starts_one_turn(client, monkeypatch):
    cid = await _first_conversation(client, monkeypatch)
    release, started, seen = asyncio.Event(), asyncio.Event(), []
    monkeypatch.setattr(chat_mod, "guest_turn", _blocked(release, started, "x", seen))
    body = {"message": "again", "conversation_id": cid}
    a = asyncio.create_task(client.post("/api/chat", json=body))
    b = asyncio.create_task(client.post("/api/chat", json=body))
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.3)
    refused = [t for t in (a, b) if t.done()]
    assert len(refused) == 1
    assert refused[0].result().status_code == 409
    assert refused[0].result().json()["detail"] == "turn_in_progress"
    release.set()
    await asyncio.gather(a, b)
    await _settle(cid)
    assert len(seen) == 1                       # one turn, not two
    roles = [r for r, _ in await _roles(client, cid)]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert not chat_mod._posting                # the claim does not leak


async def test_claim_released_when_the_post_fails(client, monkeypatch):
    r = await client.post("/api/chat", json={"message": "x", "conversation_id": 9999})
    assert r.status_code == 404
    assert not chat_mod._posting


async def test_post_into_a_live_agent_node_is_refused(client, monkeypatch):
    cid = await _first_conversation(client, monkeypatch)
    from backend import agents_run
    monkeypatch.setitem(agents_run._active_runs, cid, object())
    r = await client.post("/api/chat", json={"message": "hey", "conversation_id": cid})
    assert r.status_code == 409


# --- ROBUST-09: a failing turn says why, in the stream, the log and the transcript


async def _fail_turn(client, monkeypatch, exc):
    monkeypatch.setattr(chat_mod, "guest_turn", _raising(exc))
    seen = []
    orig = chat_mod.bus.publish
    monkeypatch.setattr(chat_mod.bus, "publish",
                        lambda chan, ev: (seen.append(ev), orig(chan, ev))[1])
    post = asyncio.create_task(client.post("/api/chat", json={"message": "hello"}))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if chat_mod._active_turns:
            break
    cid = max(chat_mod._active_turns)
    await _settle(cid)
    await asyncio.gather(post, return_exceptions=True)
    return cid, [e for e in seen if e.get("type") == "error"]


@pytest.mark.parametrize("exc", [asyncio.TimeoutError(), AssertionError(),
                                 httpx.ReadTimeout("")])
async def test_blank_exception_still_reads_as_something(client, monkeypatch, caplog, exc):
    with caplog.at_level(logging.ERROR, logger="jav3.chat"):
        cid, errs = await _fail_turn(client, monkeypatch, exc)
    assert len(errs) == 1 and errs[0]["message"]
    assert type(exc).__name__ in errs[0]["message"]
    assert any(str(cid) in r.getMessage() and r.exc_info for r in caplog.records)


async def test_failed_turn_is_in_the_transcript(client, monkeypatch):
    cid, errs = await _fail_turn(client, monkeypatch, RuntimeError("the guest went away"))
    assert errs[0]["message"] == "the guest went away"
    rows = await _roles(client, cid)
    assert [r for r, _ in rows] == ["user", "assistant"]
    assert rows[-1][1] == "(turn failed: the guest went away)"


async def test_incognito_failure_leaves_nothing(client, monkeypatch):
    monkeypatch.setattr(chat_mod, "guest_turn", _raising(RuntimeError("boom")))
    r = await client.post("/api/chat", json={"message": "secret", "ephemeral": True})
    assert r.status_code == 200
    for _ in range(100):
        if not chat_mod._active_turns:
            break
        await asyncio.sleep(0.05)
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) AS n FROM messages") as cur:
            assert (await cur.fetchone())["n"] == 0
    finally:
        await db.close()


# --- PLANS-14: a 402 is one plain message, one critical bell, a "paused" flag


BODY_402 = '{"error":{"message":"Insufficient Balance","type":"unknown_error"}}'


@pytest.fixture
async def broke(monkeypatch):
    """A DeepSeek that answers 402, counting the calls it saw."""
    from backend import provider_balance
    from backend.agent import adapters
    provider_balance.reset()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(402, text=BODY_402)
    monkeypatch.setattr(adapters, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    yield calls
    await asyncio.gather(*provider_balance._tasks)   # the bell's own DB connection
    provider_balance.reset()


async def _complete(gateway):
    async for _ in gateway.complete([{"role": "user", "content": "hi"}]):
        pass


async def _bells():
    db = await get_db()
    try:
        async with db.execute("SELECT kind, severity, summary, count FROM security_events "
                              "WHERE kind = 'provider_balance'") as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def test_402_becomes_one_plain_error_and_one_bell(client, broke):
    from backend import provider_balance
    from backend.agent.adapters import ModelError
    from backend.agent.model import ModelGateway
    gw = ModelGateway(api_key="sk-test-0123456789")
    for _ in range(3):                           # three refused calls, one outage
        with pytest.raises(ModelError) as e:
            await _complete(gw)
        assert e.value.status == 402
        assert "DeepSeek balance is empty" in str(e.value)
        assert "platform.deepseek.com" in str(e.value)
        assert "unknown_error" not in str(e.value)         # the raw JSON does not leak through
    await asyncio.sleep(0.1)
    bells = await _bells()
    # an empty account is an outage, not a breach: warn, so it never breaks
    # through do-not-disturb (it still pings by default: security.DEFAULT_KIND_MODES)
    assert len(bells) == 1 and bells[0]["severity"] == "warn"
    assert bells[0]["summary"] == str(e.value)
    assert provider_balance.is_empty() and provider_balance.reason() == str(e.value)


async def test_balance_comes_back_when_a_call_succeeds(client, broke, monkeypatch):
    from backend import provider_balance
    from backend.agent import adapters
    from backend.agent.adapters import ModelError
    from backend.agent.model import ModelGateway
    gw = ModelGateway(api_key="sk-test-0123456789")
    with pytest.raises(ModelError):
        await _complete(gw)
    assert provider_balance.is_empty("deepseek")
    sse_ok = ('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
              'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
              "data: [DONE]\n\n")
    monkeypatch.setattr(adapters, "HTTP_TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(200, text=sse_ok,
                                 headers={"content-type": "text/event-stream"})))
    await _complete(gw)
    assert not provider_balance.is_empty()


async def test_chat_final_is_the_plain_message(client, broke, monkeypatch):
    from backend import provider_balance
    from backend.agent.adapters import ModelError
    from backend.agent.model import ModelGateway
    with pytest.raises(ModelError) as e:
        await _complete(ModelGateway(api_key="sk-test-0123456789"))
    wrapped = f"(guest loop error: ModelError: ModelError: {e.value})"

    async def turn(cid, system_prompt, history, tools=None, **kw):
        yield {"type": "final", "content": wrapped}
    monkeypatch.setattr(chat_mod, "guest_turn", turn)
    r = await client.post("/api/chat", json={"message": "hello"})
    assert r.status_code == 200
    for _ in range(100):
        if not chat_mod._active_turns:
            break
        await asyncio.sleep(0.05)
    rows = await _roles(client, 1)
    assert rows[-1][1] == str(e.value)
    assert "guest loop error" not in rows[-1][1]
    assert provider_balance.tidy("something else") == "something else"


# --- ROBUST-10: the answer is published before the journal call runs --------


async def test_final_is_published_before_the_journal_call(client, monkeypatch):
    from backend import summarize
    from backend.config import settings
    from backend.db import set_state
    from backend.memory import read_project_md
    proj = settings.projects_dir / "demo"
    proj.mkdir(parents=True)
    (proj / "project.md").write_text("# Demo\n\n## Summary\nx\n\n## Journal\n")
    db = await get_db()
    await set_state(db, "active_project", "demo")
    await db.close()
    gate = asyncio.Event()

    async def slow_line(system, user, temperature=0.3):
        await gate.wait()
        return "Wrote the file."
    monkeypatch.setattr(summarize, "complete_text", slow_line)

    async def turn(cid, system_prompt, history, tools=None, **kw):
        yield {"type": "tool", "id": "t1", "name": "write_file", "args": {"path": "a.txt"}}
        yield {"type": "tool_result", "id": "t1", "name": "write_file", "ok": True,
               "result": "ok"}
        yield {"type": "final", "content": "the answer"}
    monkeypatch.setattr(chat_mod, "guest_turn", turn)
    seen = []
    orig = chat_mod.bus.publish
    monkeypatch.setattr(chat_mod.bus, "publish",
                        lambda chan, ev: (seen.append(ev), orig(chan, ev))[1])
    post = asyncio.create_task(client.post("/api/chat", json={"message": "make a.txt"}))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if any(e["type"] == "final" for e in seen) and not chat_mod._active_turns:
            break
    # the turn is over (spinner gone, POST not 409) while the journal call waits
    assert [e["content"] for e in seen if e["type"] == "final"] == ["the answer"]
    assert not chat_mod._active_turns
    assert chat_mod._background
    assert chat_mod._stop(1) is False            # nothing to stop: no marker can follow
    gate.set()
    await asyncio.gather(*chat_mod._background)
    await asyncio.wait_for(post, 5)
    assert "(auto) Wrote the file." in read_project_md("demo")
    assert [r for r, _ in await _roles(client, 1)] == ["user", "assistant"]


# --- ROBUST-11 / ROBUST-19: the incognito wipe is guarded; nothing reads it --


async def test_a_failed_wipe_does_not_brick_the_chat(client, monkeypatch):
    import sqlite3
    from backend.agent import budget
    release, started = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(chat_mod, "guest_turn", _blocked(release, started, "secret"))
    real_drop = chat_mod._drop_references

    async def locked(db, cid):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(chat_mod, "_drop_references", locked)
    post = asyncio.create_task(client.post(
        "/api/chat", json={"message": "private diagnosis", "ephemeral": True}))
    await asyncio.wait_for(started.wait(), 5)
    cid = max(chat_mod._active_turns)
    release.set()
    await asyncio.wait_for(post, 5)
    for _ in range(100):
        if cid not in chat_mod._active_turns:
            break
        await asyncio.sleep(0.05)
    assert cid not in chat_mod._active_turns             # not "running" for good
    assert budget.get(f"chat:{cid}") is None             # the rest of the cleanup ran
    assert (await client.get("/api/conversations")).json()["conversations"] == []
    # the next start finishes the wipe the turn could not
    monkeypatch.setattr(chat_mod, "_drop_references", real_drop)
    assert await chat_mod.sweep_ephemeral() == 1
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) AS n FROM conversations") as cur:
            assert (await cur.fetchone())["n"] == 0
        async with db.execute("SELECT COUNT(*) AS n FROM messages") as cur:
            assert (await cur.fetchone())["n"] == 0
    finally:
        await db.close()


async def test_incognito_chat_is_unreadable_while_it_runs(client, monkeypatch):
    release, started = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(chat_mod, "guest_turn", _blocked(release, started, "secret"))
    post = asyncio.create_task(client.post(
        "/api/chat", json={"message": "my private diagnosis", "ephemeral": True}))
    await asyncio.wait_for(started.wait(), 5)
    cid = max(chat_mod._active_turns)
    listed = (await client.get("/api/conversations")).json()["conversations"]
    assert listed == []
    assert (await client.get(f"/api/conversations/{cid}/messages")).status_code == 404
    assert (await client.get(f"/api/conversations/{cid}/info")).status_code == 404
    db = await get_db()
    try:
        async with db.execute("SELECT summary FROM conversations WHERE id = ?",
                              (cid,)) as cur:
            assert "diagnosis" not in (await cur.fetchone())["summary"]
    finally:
        await db.close()
    release.set()
    await asyncio.wait_for(post, 5)


async def test_sweep_wipes_incognito_rows_a_crash_left(client, tmp_env):
    from backend.config import settings
    from backend.db import open_conversation
    db = await get_db()
    try:
        left = await open_conversation(db, project=None, title="x", ephemeral=True)
        kept = await open_conversation(db, project=None, title="kept")
        for cid, text in ((left, "left behind by a kill"), (kept, "an ordinary chat")):
            await db.execute("INSERT INTO messages (conversation_id, role, content) "
                             "VALUES (?, 'user', ?)", (cid, text))
        await db.commit()
    finally:
        await db.close()
    assert await chat_mod.sweep_ephemeral() == 1
    db = await get_db()
    try:
        async with db.execute("SELECT id FROM conversations") as cur:
            assert [r["id"] for r in await cur.fetchall()] == [kept]
    finally:
        await db.close()
    dumps = list((settings.data_dir / "incognito").glob("*.md"))     # the recovery hatch
    assert dumps and "left behind by a kill" in dumps[0].read_text()


# --- ROBUST-20: a quiet chat stream still writes ----------------------------


async def test_chat_tail_keeps_the_connection_alive(monkeypatch):
    from backend import bus, sse
    monkeypatch.setattr(sse, "KEEPALIVE_S", 0.05)
    q = bus.subscribe("chat:77")
    it = chat_mod._tail(77, q).body_iterator
    assert await asyncio.wait_for(it.__anext__(), 2) == ": keepalive\n\n"
    bus.publish("chat:77", {"type": "final", "content": "x", "conversation_id": 77})
    frame = await asyncio.wait_for(it.__anext__(), 2)
    assert frame.startswith("data: ") and '"final"' in frame
    with pytest.raises(StopAsyncIteration):
        await it.__anext__()


# --- ROBUST-24: deleting a chat stops its turn first ------------------------


async def test_delete_stops_the_running_turn_first(client, monkeypatch):
    release, started = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(chat_mod, "guest_turn", _blocked(release, started))
    seen = []
    orig = chat_mod.bus.publish
    monkeypatch.setattr(chat_mod.bus, "publish",
                        lambda chan, ev: (seen.append(ev), orig(chan, ev))[1])
    post = asyncio.create_task(client.post("/api/chat", json={"message": "hi"}))
    await asyncio.wait_for(started.wait(), 5)
    cid = max(chat_mod._active_turns)
    r = await client.delete(f"/api/conversations/{cid}")
    assert r.status_code == 200
    assert cid not in chat_mod._active_turns             # stopped, not left running
    assert not [e for e in seen if e["type"] == "error"]  # no FOREIGN KEY error later
    assert (await client.get("/api/conversations")).json()["conversations"] == []
    await asyncio.gather(post, return_exceptions=True)
    assert not chat_mod._posting


async def test_delete_refuses_a_conversation_with_a_live_agent(client, monkeypatch):
    cid = await _first_conversation(client, monkeypatch)
    from backend import agents_run
    monkeypatch.setitem(agents_run._active_runs, cid, object())
    r = await client.delete(f"/api/conversations/{cid}")
    assert r.status_code == 409
    assert (await client.get("/api/conversations")).json()["conversations"]


# --- WEBA-14: a new message after a stop is not a request to resume ---------


async def test_message_after_a_stop_carries_the_interrupt_note(client, monkeypatch):
    release, started = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(chat_mod, "guest_turn", _blocked(release, started))
    post = asyncio.create_task(client.post(
        "/api/chat", json={"message": "append the line: third line to notes.txt"}))
    await asyncio.wait_for(started.wait(), 5)
    cid = max(chat_mod._active_turns)
    chat_mod._stop(cid)
    await asyncio.gather(post, return_exceptions=True)
    await _settle(cid)
    assert (await _roles(client, cid))[-1][1] == chat_mod.INTERRUPTED_MARKER
    seen, go, begun = [], asyncio.Event(), asyncio.Event()
    go.set()
    monkeypatch.setattr(chat_mod, "guest_turn", _blocked(go, begun, "ok3", seen))
    r = await client.post("/api/chat", json={"message": "Reply with the single word: ok3",
                                             "conversation_id": cid})
    assert r.status_code == 200
    await _settle(cid)
    h = seen[0]["history"]
    assert h[-1]["content"] == "Reply with the single word: ok3"
    assert h[-2] == {"role": "user", "content": chat_mod.INTERRUPT_NOTE}
    assert "third line" in h[-3]["content"]
    # ...and only that once: the turn after a normal answer carries no note
    seen2 = []
    monkeypatch.setattr(chat_mod, "guest_turn", _blocked(go, begun, "fine", seen2))
    await client.post("/api/chat", json={"message": "thanks", "conversation_id": cid})
    await _settle(cid)
    assert all(m["content"] != chat_mod.INTERRUPT_NOTE for m in seen2[0]["history"])
