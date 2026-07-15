"""Phase 2: the host vsock model gateway. Offline — the transport is scripted and
the guest is a plain AF_UNIX socketpair, so no vsock/VM is needed. Proves the
gateway reshapes a `model_call` into streamed events, keeps the key host-side, and
handles ping / unknown / bad requests."""
import asyncio
import json
import socket

from backend.agent.model import Model, model
from backend.vm.gateway_server import handle_conn


def _script_transport(monkeypatch, *, content="PONG", usage=None):
    async def fake_stream_once(self, base, key, payload):
        yield {"type": "raw", "content": content, "tool_calls": [], "usage": usage}
    monkeypatch.setattr(Model, "_stream_once", fake_stream_once)


async def _roundtrip(monkeypatch, request: dict, register_op=True):
    from backend.agent import budget as bmod
    from backend.agent.budget import Budget
    monkeypatch.setattr(model, "api_key", "sk-secret")
    monkeypatch.setattr(model.transport, "api_key", "sk-secret")
    op_id = request.get("op_id")
    # op_id pinning: the gateway only serves turns the host registered. Tests that
    # want to hit a real model_call register first; register_op=False exercises the
    # rejection path.
    if op_id and register_op:
        bmod.register(op_id, Budget(10**9, 10**9))
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    loop = asyncio.get_running_loop()
    server = asyncio.create_task(handle_conn(loop, b))
    try:
        await loop.sock_sendall(a, (json.dumps(request) + "\n").encode())
        data = b""
        try:
            while True:
                chunk = await asyncio.wait_for(loop.sock_recv(a, 65536), timeout=5)
                if not chunk:
                    break
                data += chunk
                if any(t in data for t in (b'"message"', b'"error"', b'"pong"',
                                           b'"broker_result"')):
                    break
        except asyncio.TimeoutError:
            pass
        a.close()
        await asyncio.wait_for(server, timeout=5)
        return [json.loads(line) for line in data.splitlines() if line.strip()]
    finally:
        if op_id and register_op:
            bmod.release(op_id)


async def test_model_call_streams_a_message(monkeypatch):
    _script_transport(monkeypatch, content="PONG",
                      usage={"prompt_tokens": 3, "completion_tokens": 1})
    events = await _roundtrip(monkeypatch, {
        "op": "model_call", "op_id": "vm-x",
        "messages": [{"role": "user", "content": "hi"}]})
    msg = [e for e in events if e.get("type") == "message"]
    assert msg and msg[0]["content"] == "PONG"


async def test_key_never_crosses_the_boundary(monkeypatch):
    _script_transport(monkeypatch, content="ok")
    events = await _roundtrip(monkeypatch, {
        "op": "model_call", "op_id": "vm-x",
        "messages": [{"role": "user", "content": "hi"}]})
    assert "sk-secret" not in json.dumps(events)


async def test_ping_pong(monkeypatch):
    events = await _roundtrip(monkeypatch, {"op": "ping"})
    assert events and events[0].get("type") == "pong"


async def test_unknown_op_is_an_error(monkeypatch):
    events = await _roundtrip(monkeypatch, {"op": "frobnicate"})
    assert events and events[0]["type"] == "error" and events[0]["error"] == "unknown_op"


async def test_bad_json_is_an_error(monkeypatch):
    monkeypatch.setattr(model, "api_key", "sk-secret")
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    loop = asyncio.get_running_loop()
    server = asyncio.create_task(handle_conn(loop, b))
    await loop.sock_sendall(a, b"{not json\n")
    chunk = await asyncio.wait_for(loop.sock_recv(a, 65536), timeout=5)
    a.close()
    await asyncio.wait_for(server, timeout=5)
    ev = json.loads(chunk.splitlines()[0])
    assert ev["type"] == "error" and ev["error"] == "bad_json"


# --- Phase 3 M2: op_id pinning + the tool broker ------------------------------

async def test_unregistered_op_id_is_rejected(monkeypatch):
    # a compromised guest inventing an op_id gets nothing — the gateway only
    # serves turns the host registered.
    _script_transport(monkeypatch)
    events = await _roundtrip(monkeypatch, {
        "op": "model_call", "op_id": "not-registered",
        "messages": [{"role": "user", "content": "hi"}]}, register_op=False)
    assert events and events[0]["type"] == "error"
    assert events[0]["error"] == "unknown_op_id"


async def test_tool_broker_call_dispatches_and_stamps_taint(monkeypatch):
    from backend.agent.tools import registry
    from backend.vm import broker

    async def fake_dispatch(name, args):
        return f"ran {name} args={args}"
    monkeypatch.setattr(registry, "dispatch", fake_dispatch)

    broker.register_turn(broker.TurnEnvelope(op_id="op-b", web_session="ws"))
    try:
        events = await _roundtrip(monkeypatch, {
            "op": "tool_broker_call", "op_id": "op-b",
            "name": "web_read", "args": {"url": "x"}}, register_op=False)
    finally:
        broker.release_turn("op-b")
    ev = events[0]
    assert ev["type"] == "broker_result"
    assert "ran web_read" in ev["result"]
    assert ev["taint"] == "untrusted"          # web_read pulls untrusted content


async def test_tool_broker_call_unregistered_op_rejected(monkeypatch):
    events = await _roundtrip(monkeypatch, {
        "op": "tool_broker_call", "op_id": "nope", "name": "read_file",
        "args": {}}, register_op=False)
    assert events and events[0]["type"] == "error" and events[0]["error"] == "unknown_op_id"
