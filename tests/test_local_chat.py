"""/local chats, server side: a conversation opened with a `local` object gets
the local_* toolset and a prompt saying where it is; a local_* tool call
publishes `local_tool` on the chat channel and waits for the client's POST to
/api/chat/{id}/local_result; stop and a timeout both release the wait.

The guest is replaced by a fake guest_turn that does what the real one's
broker path does: register the envelope, then call broker_dispatch with the
model's call id (backend/vm/gateway_server passes it through)."""
import asyncio
import contextlib
import json

import httpx
import pytest

from backend import localexec
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds

SPEC = {"cwd": "/home/op/src/thing", "hostname": "opbox", "os": "Linux 6.9",
        "shell": "/bin/zsh"}


@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    localexec.reset_for_tests()
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
    localexec.reset_for_tests()


def _local_turn(calls, seen, results):
    """A guest turn that brokers each (name, args, call_id) in `calls` and
    finishes with their results joined."""
    async def turn(cid, system_prompt, history, *, envelope=None, op_id=None,
                   tool_specs=None, **kw):
        from backend.vm import broker
        seen["system_prompt"] = system_prompt
        seen["tools"] = [t["function"]["name"] for t in tool_specs or []]
        broker.register_turn(envelope)
        try:
            for name, args, call_id in calls:
                yield {"type": "tool", "id": call_id, "name": name, "args": args}
                res = await broker.broker_dispatch(op_id, name, args, call_id=call_id)
                results.append(res)
                yield {"type": "tool_result", "id": call_id, "name": name,
                       "ok": not res["result"].startswith("error:"),
                       "result": res["result"]}
            yield {"type": "final",
                   "content": " | ".join(r["result"] for r in results)}
        finally:
            broker.release_turn(op_id)
    return turn


async def _wait_pending(n=1):
    for _ in range(200):
        if len(localexec._pending) >= n:
            return list(localexec._pending.values())
        await asyncio.sleep(0.01)
    raise AssertionError("no local call became pending")


async def _settle(cid):
    from backend import chat as chat_mod
    task = chat_mod._active_turns.get(cid)
    if task:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_local_turn_round_trip(client, monkeypatch):
    from backend import chat as chat_mod
    seen, results = {}, []
    monkeypatch.setattr(chat_mod, "guest_turn", _local_turn(
        [("local_read_file", {"path": "a.py"}, "call_7")], seen, results))
    post = asyncio.create_task(client.post("/api/chat", json={
        "message": "read a.py", "confirm_peak": True, "local": SPEC}))
    (p,) = await _wait_pending()
    cid = p.conversation_id
    # the event went out on the chat channel under the model's own call id
    # (the handler fills the defaults in, so the client never has to guess them)
    assert p.event == {"type": "local_tool", "id": "call_7", "name": "local_read_file",
                       "args": {"path": "a.py", "offset": 1, "limit": 2000}}
    # the toolset is the local one: no sandbox file/run tools, no project tools
    assert "local_shell" in seen["tools"] and "local_read_file" in seen["tools"]
    for gone in ("read_file", "write_file", "run_code", "git_status", "load_project"):
        assert gone not in seen["tools"]
    assert "web_read" in seen["tools"] and "memory_write" in seen["tools"]
    assert "opbox (Linux 6.9)" in seen["system_prompt"]
    assert "/home/op/src/thing" in seen["system_prompt"]
    assert "approve" in seen["system_prompt"]

    # a re-attaching client is handed the call it missed
    tail = asyncio.create_task(client.get(f"/api/chat/{cid}/stream"))
    await asyncio.sleep(0.05)
    # a wrong id is a 404, and does not consume the real one
    r = await client.post(f"/api/chat/{cid}/local_result",
                          json={"id": "nope", "ok": True, "result": "x"})
    assert r.status_code == 404
    r = await client.post(f"/api/chat/{cid}/local_result",
                          json={"id": "call_7", "ok": True, "result": "     1\tx = 1"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    body = (await asyncio.wait_for(post, 5)).text
    assert "x = 1" in body
    tail_text = (await asyncio.wait_for(tail, 5)).text
    assert '"local_tool"' in tail_text and '"call_7"' in tail_text
    await _settle(cid)
    # untrusted, like any machine-authored output
    assert results[0]["taint"] == "untrusted"
    # answered once: a second answer finds nothing waiting
    r = await client.post(f"/api/chat/{cid}/local_result",
                          json={"id": "call_7", "ok": True, "result": "again"})
    assert r.status_code == 404
    # the row is marked, and the client can read it back
    info = (await client.get(f"/api/conversations/{cid}/info")).json()
    assert info["local"] == SPEC
    assert info["project"] is None


async def test_client_error_and_cap_and_secret_scrub(client, monkeypatch, tmp_env):
    from backend import chat as chat_mod
    from backend import secrets as secrets_mod
    secrets_mod.save({"API_KEY": "sk-verysecretvalue"})
    seen, results = {}, []
    monkeypatch.setattr(chat_mod, "guest_turn", _local_turn(
        [("local_shell", {"command": "ls"}, "c1"),
         ("local_read_file", {"path": "big"}, "c2")], seen, results))
    post = asyncio.create_task(client.post("/api/chat", json={
        "message": "go", "confirm_peak": True, "local": SPEC}))
    (p,) = await _wait_pending()
    cid = p.conversation_id
    await client.post(f"/api/chat/{cid}/local_result",
                      json={"id": "c1", "ok": False, "result": "the operator said no"})
    await _wait_pending()
    big = "k=sk-verysecretvalue\n" + "y" * 300_000
    await client.post(f"/api/chat/{cid}/local_result",
                      json={"id": "c2", "ok": True, "result": big})
    await asyncio.wait_for(post, 5)
    await _settle(cid)
    assert results[0]["result"] == "error: the operator said no"
    out = results[1]["result"]
    assert "sk-verysecretvalue" not in out and "{{secret:API_KEY}}" in out
    assert len(out) < localexec.RESULT_CAP + 200


async def test_write_carrying_a_secret_is_refused_before_the_client(client, monkeypatch):
    from backend import chat as chat_mod
    from backend import secrets as secrets_mod
    secrets_mod.save({"API_KEY": "sk-verysecretvalue"})
    seen, results = {}, []
    monkeypatch.setattr(chat_mod, "guest_turn", _local_turn(
        [("local_write_file", {"path": "k", "content": "sk-verysecretvalue"}, "c1")],
        seen, results))
    r = await client.post("/api/chat", json={"message": "go", "confirm_peak": True,
                                             "local": SPEC})
    assert r.status_code == 200
    assert results[0]["result"].startswith("error: refused")
    assert not localexec._pending


async def test_stop_cancels_a_waiting_local_call(client, monkeypatch):
    from backend import chat as chat_mod
    seen, results = {}, []
    monkeypatch.setattr(chat_mod, "guest_turn", _local_turn(
        [("local_shell", {"command": "sleep 999"}, "c1")], seen, results))
    post = asyncio.create_task(client.post("/api/chat", json={
        "message": "go", "confirm_peak": True, "local": SPEC}))
    (p,) = await _wait_pending()
    cid = p.conversation_id
    # the fake broker call runs in the turn's task here; in production it runs
    # on the gateway's task, which only the cancel in the turn's finally reaches
    fut = p.fut
    r = await client.post(f"/api/chat/{cid}/stop")
    assert r.json()["stopped"] is True
    await _settle(cid)
    await asyncio.wait_for(post, 5)
    assert fut.done()
    assert not localexec._pending
    r = await client.post(f"/api/chat/{cid}/local_result",
                          json={"id": "c1", "ok": True, "result": "late"})
    assert r.status_code == 404


async def test_cancel_conversation_errors_a_waiter_on_another_task(client, monkeypatch):
    """The production shape: the broker call waits on the GATEWAY's task, not
    the turn's, so it is the explicit cancel that ends it."""
    from backend import chat as chat_mod, runtime
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary, local) VALUES (?, ?)",
                               ("x", json.dumps(SPEC)))
        await db.commit()
        cid = cur.lastrowid
    finally:
        await db.close()
    monkeypatch.setitem(chat_mod._active_turns, cid, object())
    runtime.conversation_id.set(cid)
    runtime.event_chan.set(f"chat:{cid}")
    waiter = asyncio.create_task(localexec.call("local_shell", {"command": "ls"}))
    await _wait_pending()
    assert localexec.cancel_conversation(cid) == 1
    out = await asyncio.wait_for(waiter, 5)
    assert out.startswith("error:") and "stopped" in out


async def test_unanswered_call_times_out(client, monkeypatch):
    from backend import chat as chat_mod
    monkeypatch.setattr(localexec, "CALL_TIMEOUT_S", 0.2)
    seen, results = {}, []
    monkeypatch.setattr(chat_mod, "guest_turn", _local_turn(
        [("local_list_files", {"path": "."}, "c1")], seen, results))
    r = await client.post("/api/chat", json={"message": "go", "confirm_peak": True,
                                             "local": SPEC})
    assert r.status_code == 200
    assert results[0]["result"].startswith("error: opbox did not answer")
    assert not localexec._pending


async def test_local_tools_refused_outside_a_local_chat(client, monkeypatch):
    from backend import chat as chat_mod
    seen, results = {}, []
    monkeypatch.setattr(chat_mod, "guest_turn", _local_turn(
        [("local_shell", {"command": "id"}, "c1")], seen, results))
    await client.post("/api/chat", json={"message": "go", "confirm_peak": True})
    # not offered, and a guest that brokers the name anyway is refused
    assert not any(t.startswith("local_") for t in seen["tools"])
    assert "read_file" in seen["tools"] or "memory_read" in seen["tools"]
    assert results[0]["result"].startswith("error: this chat is not a local session")
    assert not localexec._pending


async def test_only_the_opening_actor_may_answer(client):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    localexec._pending[(5, "c1")] = localexec._Pending(5, "device:3", {}, fut)
    assert localexec.resolve(5, "c1", True, "x", "session") == "forbidden"
    assert localexec.resolve(5, "c1", True, "x", "device:4") == "forbidden"
    assert not fut.done()
    assert localexec.resolve(5, "c1", True, "x", "device:3") == "ok"
    assert fut.result() == (True, "x")


@pytest.mark.parametrize("bad", [
    "nope", {"cwd": "relative/dir", "hostname": "h", "os": "o"},
    {"cwd": "/x", "hostname": "", "os": "o"},
    {"cwd": "/x", "hostname": "h" * 500, "os": "o"},
])
async def test_bad_local_object_is_a_400(client, bad):
    r = await client.post("/api/chat", json={"message": "go", "confirm_peak": True,
                                             "local": bad})
    assert r.status_code == 400 or r.status_code == 422


def test_clean_spec_strips_control_characters():
    s = localexec.clean_spec({"cwd": "/a\nIgnore previous", "hostname": "h\x1b[31m",
                              "os": "Linux"})
    assert "\n" not in s["cwd"] and "\x1b" not in s["hostname"]
    assert s["shell"] == "sh"


# --- the call id's path: loop -> registry.call_id -> vsock -> runtime ---------

async def test_loop_hands_each_call_its_id(tmp_env, monkeypatch):
    from backend.agent import loop as loop_mod
    from backend.agent.tools import registry

    class Model:
        n = 0

        async def complete(self, messages, tools=None, **kw):
            if self.n == 0:
                self.n = 1
                yield {"type": "message", "content": "", "usage": None, "tool_calls": [
                    {"id": "call_a", "type": "function",
                     "function": {"name": "t1", "arguments": "{}"}},
                    {"id": "call_b", "type": "function",
                     "function": {"name": "t2", "arguments": "{}"}}]}
            else:
                yield {"type": "message", "content": "done", "tool_calls": [],
                       "usage": None}

    got = []

    async def dispatch(name, args):
        got.append((name, registry.call_id.get()))
        return "ok"

    await init_db()
    monkeypatch.setattr(loop_mod, "model", Model())
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())
    evs = []
    async for ev in loop_mod.run_turn(
            1, "s", [{"role": "user", "content": "go"}],
            tools=[{"type": "function", "function": {"name": "t1", "parameters": {}}}]):
        evs.append(ev)
    assert evs, evs
    assert got == [("t1", "call_a"), ("t2", "call_b")]
    assert registry.call_id.get() is None


@pytest.mark.parametrize("sent,want", [("call_x", "call_x"), ({"a": 1}, None),
                                        ("x" * 500, None), (None, None)])
async def test_gateway_restores_the_call_id(tmp_env, monkeypatch, sent, want):
    import contextvars
    import socket

    from backend import runtime
    from backend.agent.tools import registry
    from backend.vm import broker
    from backend.vm.gateway_server import handle_conn

    seen = []

    async def dispatch(name, args):
        seen.append(runtime.tool_call_id.get())
        return "fine"

    monkeypatch.setattr(registry, "dispatch", dispatch)
    broker.register_turn(broker.TurnEnvelope(op_id="op-l", conversation_id=1))
    broker.register_token("op-l", "tok")
    loop = asyncio.get_running_loop()
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    server = asyncio.create_task(handle_conn(loop, b), context=contextvars.Context())
    try:
        await loop.sock_sendall(a, (json.dumps(
            {"op": "tool_broker_call", "op_id": "op-l", "op_token": "tok",
             "name": "memory_read", "args": {}, "call_id": sent}) + "\n").encode())
        data = b""
        while b"\n" not in data:
            data += await asyncio.wait_for(loop.sock_recv(a, 65536), timeout=10)
        assert json.loads(data)["result"] == "fine"
    finally:
        a.close()
        await asyncio.wait_for(server, timeout=5)
        broker.release_turn("op-l")
        broker.release_token("op-l")
    assert seen == [want]
