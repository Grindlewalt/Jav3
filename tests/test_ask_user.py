"""ask_user: a brokered tool call publishes `ask_user` on the chat channel and
waits for POST /api/chat/{id}/answer; skip, stop, bad answers and an agent's
ask shown in its parent's conversation (backend/operator_ask.py)."""
import asyncio
import contextlib

import httpx
import pytest

from backend import bus, operator_ask, runtime
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import STATIC_BEHAVIOR, ensure_memory_seeds

Q = [{"question": "Which database?", "options": ["SQLite", "Postgres"]},
     {"question": "Which extras?", "options": ["auth", "admin", "api"],
      "multi_select": True}]


@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    operator_ask.reset_for_tests()
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
    operator_ask.reset_for_tests()


def _turn(calls, results):
    async def turn(cid, system_prompt, history, *, envelope=None, op_id=None,
                   tool_specs=None, **kw):
        from backend.vm import broker
        results.append({"tools": [t["function"]["name"] for t in tool_specs or []],
                        "prompt": system_prompt})
        broker.register_turn(envelope)
        try:
            out = []
            for name, args in calls:
                res = await broker.broker_dispatch(op_id, name, args, call_id="c1")
                out.append(res["result"])
            results.append(out)
            yield {"type": "final", "content": " | ".join(out)}
        finally:
            broker.release_turn(op_id)
    return turn


async def _wait_ask():
    for _ in range(300):
        if operator_ask._pending:
            return next(iter(operator_ask._pending.values()))
        await asyncio.sleep(0.01)
    raise AssertionError("no ask became pending")


async def _settle(cid):
    from backend import chat as chat_mod
    task = chat_mod._active_turns.get(cid)
    if task:
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_prompt_mentions_ask_user():
    assert "ask_user" in STATIC_BEHAVIOR and "instead of guessing" in STATIC_BEHAVIOR


def test_clean_questions_and_answers():
    assert isinstance(operator_ask.clean_questions([]), str)
    assert isinstance(operator_ask.clean_questions([{"question": "x", "options": ["a"]}]), str)
    assert isinstance(operator_ask.clean_questions(
        [{"question": "x", "options": list("abcdef")}]), str)
    qs = operator_ask.clean_questions(Q)
    assert qs[0] == {"question": "Which database?", "options": ["SQLite", "Postgres"],
                     "multi_select": False}
    ok = operator_ask.clean_answers(qs, [{"selected": ["SQLite"]},
                                         {"selected": ["auth", "api"], "text": "and docs"}])
    assert ok[1] == {"selected": ["auth", "api"], "text": "and docs"}
    # typed text alone is an answer (the free-text option)
    assert operator_ask.clean_answers(qs, [{"text": "MySQL"}, {"selected": ["auth"]}])
    # single-select: one pick OR text; unknown labels; wrong count; empty
    assert operator_ask.clean_answers(qs, [{"selected": ["SQLite", "Postgres"]},
                                           {"selected": ["auth"]}]) is None
    assert operator_ask.clean_answers(qs, [{"selected": ["SQLite"], "text": "x"},
                                           {"selected": ["auth"]}]) is None
    assert operator_ask.clean_answers(qs, [{"selected": ["Oracle"]},
                                           {"selected": ["auth"]}]) is None
    assert operator_ask.clean_answers(qs, [{"selected": ["SQLite"]}]) is None
    assert operator_ask.clean_answers(qs, [{}, {"selected": ["auth"]}]) is None


async def test_ask_round_trip(client, monkeypatch):
    from backend import chat as chat_mod
    results = []
    monkeypatch.setattr(chat_mod, "guest_turn", _turn([("ask_user", {"questions": Q})],
                                                      results))
    post = asyncio.create_task(client.post("/api/chat", json={
        "message": "build it", "confirm_peak": True}))
    a = await _wait_ask()
    cid = a.conversation_id
    assert "ask_user" in results[0]["tools"]
    assert a.event["type"] == "ask_user" and a.event["questions"][1]["multi_select"]
    # shows in notifications and the agents tree needs
    n = (await client.get("/api/notifications")).json()
    assert n["asks"][0]["id"] == a.id and n["count"] >= 1
    # a re-attaching client is handed the pending ask
    tail = asyncio.create_task(client.get(f"/api/chat/{cid}/stream"))
    await asyncio.sleep(0.05)
    r = await client.post(f"/api/chat/{cid}/answer", json={"id": "nope", "skipped": True})
    assert r.status_code == 404
    r = await client.post(f"/api/chat/{cid}/answer",
                          json={"id": a.id, "answers": [{"selected": ["Oracle"]},
                                                        {"selected": ["auth"]}]})
    assert r.status_code == 422
    r = await client.post(f"/api/chat/{cid}/answer", json={
        "id": a.id, "answers": [{"selected": ["Postgres"]},
                                {"selected": ["auth", "api"], "text": "and docs"}]})
    assert r.status_code == 200
    body = (await asyncio.wait_for(post, 5)).text
    assert "Postgres" in body and "(typed) and docs" in body
    tail_text = (await asyncio.wait_for(tail, 5)).text
    assert '"ask_user"' in tail_text and a.id in tail_text
    await _settle(cid)
    r = await client.post(f"/api/chat/{cid}/answer", json={"id": a.id, "skipped": True})
    assert r.status_code == 404


async def test_skip_tells_the_agent(client, monkeypatch):
    from backend import chat as chat_mod
    results = []
    monkeypatch.setattr(chat_mod, "guest_turn", _turn([("ask_user", {"questions": Q[:1]})],
                                                      results))
    post = asyncio.create_task(client.post("/api/chat", json={
        "message": "go", "confirm_peak": True}))
    a = await _wait_ask()
    r = await client.post(f"/api/chat/{a.conversation_id}/answer",
                          json={"id": a.id, "skipped": True})
    assert r.status_code == 200
    await asyncio.wait_for(post, 5)
    await _settle(a.conversation_id)
    assert "SKIPPED" in results[-1][0]


async def test_stop_releases_the_ask(client, monkeypatch):
    from backend import chat as chat_mod
    results = []
    monkeypatch.setattr(chat_mod, "guest_turn", _turn([("ask_user", {"questions": Q[:1]})],
                                                      results))
    post = asyncio.create_task(client.post("/api/chat", json={
        "message": "go", "confirm_peak": True}))
    a = await _wait_ask()
    await client.post(f"/api/chat/{a.conversation_id}/stop")
    await asyncio.wait_for(post, 5)
    await _settle(a.conversation_id)
    assert not operator_ask._pending


async def test_agent_ask_shows_in_parent_conversation(client):
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('orch')")
        root = cur.lastrowid
        cur = await db.execute("INSERT INTO conversations (summary, kind, "
                               "parent_conversation_id) VALUES ('w', 'subagent', ?)", (root,))
        child = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    q_root = bus.subscribe(f"chat:{root}")
    q_node = bus.subscribe(f"node:{child}")
    t1 = runtime.conversation_id.set(child)
    t2 = runtime.event_chan.set(f"node:{child}")
    try:
        task = asyncio.create_task(operator_ask.ask(operator_ask.clean_questions(Q[:1])))
        a = await _wait_ask()
    finally:
        runtime.conversation_id.reset(t1)
        runtime.event_chan.reset(t2)
    assert (await q_root.get())["id"] == a.id
    assert (await q_node.get())["conversation_id"] == child
    assert operator_ask.pending_events(root)[0]["id"] == a.id
    # answered from the orchestrator's conversation
    r = await client.post(f"/api/chat/{root}/answer",
                          json={"id": a.id, "answers": [{"text": "neither"}]})
    assert r.status_code == 200
    assert (await task) == {"answers": [{"selected": [], "text": "neither"}]}
    assert (await q_root.get())["type"] == "ask_done"
