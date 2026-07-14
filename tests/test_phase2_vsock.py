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


async def _roundtrip(monkeypatch, request: dict):
    monkeypatch.setattr(model, "api_key", "sk-secret")
    monkeypatch.setattr(model.transport, "api_key", "sk-secret")
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    loop = asyncio.get_running_loop()
    server = asyncio.create_task(handle_conn(loop, b))
    await loop.sock_sendall(a, (json.dumps(request) + "\n").encode())
    data = b""
    try:
        while True:
            chunk = await asyncio.wait_for(loop.sock_recv(a, 65536), timeout=5)
            if not chunk:
                break
            data += chunk
            if any(t in data for t in (b'"message"', b'"error"', b'"pong"')):
                break
    except asyncio.TimeoutError:
        pass
    a.close()
    await asyncio.wait_for(server, timeout=5)
    return [json.loads(line) for line in data.splitlines() if line.strip()]


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
