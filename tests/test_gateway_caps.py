"""Hard caps on what the untrusted guest can make the host gateway hold (ROBUST-06):
a request line has a size cap by op, a box has a connection cap and a byte budget,
an idle or stalled connection is closed, a reply nobody reads is dropped, and a
guest that keeps sending refused requests is cut off. A trip answers the guest with
an error naming the cap and leaves a refusal row and a security event; normal
requests, big tool results and image results still pass. Offline: AF_UNIX
socketpairs."""
import asyncio
import json
import socket

import pytest

from backend.agent import budget as bmod
from backend.agent import imageresult
from backend.agent.budget import Budget
from backend.agent.model import Model, model
from backend.agent.tools import registry
from backend.db import get_db, init_db
from backend.vm import broker, gateway_log, gateway_server as gw


@pytest.fixture(autouse=True)
def fresh_caps():
    gateway_log._buckets.clear()
    gateway_log._trips.clear()
    gw._hits.clear()
    yield
    # every connection released its claim
    assert gw._totals.conns == 0 and gw._totals.held == 0
    assert not gw._meters
    assert not broker._envelopes and not broker._inflight and not broker._op_tokens


def _pair():
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    return a, b


async def _rows(sql):
    db = await get_db()
    try:
        async with db.execute(sql) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _read_all(loop, a, timeout=5):
    data = b""
    try:
        while True:
            chunk = await asyncio.wait_for(loop.sock_recv(a, 1 << 20), timeout)
            if not chunk:
                break
            data += chunk
    except (ConnectionError, OSError):
        pass
    return [json.loads(x) for x in data.splitlines() if x.strip()]


async def _flood(loop, a, total, chunk=1 << 16):
    """Send `total` bytes with no newline; stop when the host hangs up."""
    try:
        sent = 0
        while sent < total:
            await loop.sock_sendall(a, b"A" * chunk)
            sent += chunk
    except (ConnectionError, OSError):
        pass


async def test_line_without_newline_is_cut_at_the_cap(tmp_env):
    await init_db()
    loop = asyncio.get_running_loop()
    a, b = _pair()
    server = asyncio.create_task(gw.handle_conn(loop, b))
    flood = asyncio.create_task(_flood(loop, a, 8 << 20))
    await asyncio.wait_for(server, 5)                 # closed, not still reading
    events = await _read_all(loop, a)
    flood.cancel()
    a.close()
    assert events and events[0]["error"] == "request_too_large"
    # a refusal row and a security event name the cap
    assert [r["reason"] for r in await _rows("SELECT reason FROM gateway_refusals")] == \
        ["cap:request_too_large"]
    ev = await _rows("SELECT kind, severity, summary FROM security_events")
    assert len(ev) == 1 and ev[0]["kind"] == "gateway_cap"
    assert "request_too_large" in ev[0]["summary"]


async def test_the_cap_follows_the_op_named_first(tmp_env):
    """A 2 MB line that names an unknown op is over the default cap; the same
    size for model_call is a normal context."""
    await init_db()
    loop = asyncio.get_running_loop()
    a, b = _pair()
    server = asyncio.create_task(gw.handle_conn(loop, b))
    big = json.dumps({"op": "ping", "pad": "x" * (2 << 20)}) + "\n"
    send = asyncio.create_task(_flood_bytes(loop, a, big.encode()))
    await asyncio.wait_for(server, 5)
    events = await _read_all(loop, a)
    send.cancel()
    a.close()
    assert events and events[0]["error"] == "request_too_large"


async def _flood_bytes(loop, a, data):
    try:
        await loop.sock_sendall(a, data)
    except (ConnectionError, OSError):
        pass


def _script_model(monkeypatch, seen):
    async def fake_stream_once(self, base, key, payload):
        seen.append(len(json.dumps(payload["messages"])))
        yield {"type": "raw", "content": "PONG", "tool_calls": [],
               "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
    monkeypatch.setattr(Model, "_stream_once", fake_stream_once)
    monkeypatch.setattr(model, "api_key", "sk-secret")
    monkeypatch.setattr(model.transport, "api_key", "sk-secret")


async def _one_request(loop, req, timeout=15):
    a, b = _pair()
    server = asyncio.create_task(gw.handle_conn(loop, b))
    await loop.sock_sendall(a, (json.dumps(req) + "\n").encode())
    data = b""
    while b"\n" not in data:
        chunk = await asyncio.wait_for(loop.sock_recv(a, 1 << 20), timeout)
        if not chunk:
            break
        data += chunk
    a.close()
    await asyncio.wait_for(server, 5)
    return json.loads(data.split(b"\n", 1)[0])


async def test_a_large_model_call_context_still_passes(tmp_env, monkeypatch):
    """~6 MB of context (a long turn with a screenshot in it) is far over the
    default cap and well inside model_call's."""
    await init_db()
    seen = []
    _script_model(monkeypatch, seen)
    bmod.register("vm-big", Budget(10**9, 10**9))
    broker.register_token("vm-big", "tok")
    try:
        ev = await _one_request(asyncio.get_running_loop(), {
            "op": "model_call", "op_id": "vm-big", "op_token": "tok",
            "messages": [{"role": "user", "content": "y" * (6 << 20)}]})
    finally:
        bmod.release("vm-big")
        broker.release_token("vm-big")
    assert ev["type"] == "message" and ev["content"] == "PONG"
    assert seen and seen[0] > 6 << 20


async def test_big_tool_results_and_images_pass(tmp_env, monkeypatch):
    """The replies are not capped: a 3 MB tool result and a 4.4 MB image."""
    await init_db()
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 4_400_000

    async def fake_dispatch(name, args):
        if name == "shot":
            return imageresult.with_inline("shot taken", png, caption="cap")
        return "r" * 3_000_000

    async def no_gate(name, args):
        return None
    from backend import permissions
    monkeypatch.setattr(registry, "dispatch", fake_dispatch)
    monkeypatch.setattr(permissions, "gate", no_gate)
    broker.register_turn(broker.TurnEnvelope(op_id="guest:701", conversation_id=701))
    broker.register_token("guest:701", "tok")
    loop = asyncio.get_running_loop()
    try:
        big = await _one_request(loop, {"op": "tool_broker_call", "op_id": "guest:701",
                                        "op_token": "tok", "name": "read_x", "args": {}})
        shot = await _one_request(loop, {"op": "tool_broker_call", "op_id": "guest:701",
                                         "op_token": "tok", "name": "shot", "args": {}})
    finally:
        broker.release_token("guest:701")
        broker.release_turn("guest:701")
    assert big["type"] == "broker_result" and len(big["result"]) == 3_000_000
    assert shot["type"] == "broker_result" and shot["image"]["mime"] == "image/png"
    assert len(shot["image"]["b64"]) > 5_800_000


async def test_broker_call_line_cap_is_its_own(tmp_env):
    """A tool call's arguments are one model output: over 8 MB is refused."""
    await init_db()
    loop = asyncio.get_running_loop()
    a, b = _pair()
    server = asyncio.create_task(gw.handle_conn(loop, b))
    head = b'{"op": "tool_broker_call", "op_id": "x", "args": {"c": "'
    async def send():
        await _flood_bytes(loop, a, head)
        await _flood(loop, a, 10 << 20)
    s = asyncio.create_task(send())
    await asyncio.wait_for(server, 10)
    events = await _read_all(loop, a)
    s.cancel()
    a.close()
    assert events[0]["error"] == "request_too_large"


async def test_connections_per_box_are_capped(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(gw, "MAX_CONNS_PER_BOX", 3)
    loop = asyncio.get_running_loop()
    held, tasks = [], []
    for _ in range(3):
        a, b = _pair()
        held.append(a)
        tasks.append(asyncio.create_task(gw.handle_conn(loop, b, peer_cid=42)))
    await asyncio.sleep(0.1)
    a, b = _pair()
    over = asyncio.create_task(gw.handle_conn(loop, b, peer_cid=42))
    events = await _read_all(loop, a)
    await asyncio.wait_for(over, 5)
    a.close()
    assert events[0]["error"] == "too_many_connections"
    # another box (another CID) is not affected
    a2, b2 = _pair()
    other = asyncio.create_task(gw.handle_conn(loop, b2, peer_cid=43))
    await loop.sock_sendall(a2, b'{"op": "ping"}\n')
    assert json.loads(await loop.sock_recv(a2, 4096))["type"] == "pong"
    a2.close()
    await asyncio.wait_for(other, 5)
    for a in held:
        a.close()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert [r["reason"] for r in await _rows("SELECT reason FROM gateway_refusals")] == \
        ["cap:too_many_connections"]


async def test_box_byte_budget_is_shared_by_its_connections(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(gw, "BOX_BUFFER_BYTES", 600_000)
    monkeypatch.setattr(gw, "MAX_LINE_BYTES", 500_000)
    loop = asyncio.get_running_loop()
    holders = []
    for _ in range(2):                              # two half-sent lines of 250 KB
        a, b = _pair()
        t = asyncio.create_task(gw.handle_conn(loop, b, peer_cid=7))
        await loop.sock_sendall(a, b"B" * 250_000)
        holders.append((a, t))
    await asyncio.sleep(0.2)
    a, b = _pair()
    third = asyncio.create_task(gw.handle_conn(loop, b, peer_cid=7))
    await _flood_bytes(loop, a, b"B" * 250_000)     # would be 750 KB of 600 KB
    events = await _read_all(loop, a)
    await asyncio.wait_for(third, 5)
    a.close()
    assert events[0]["error"] == "buffer_budget"
    for a, t in holders:
        a.close()
        await asyncio.wait_for(t, 5)


async def test_idle_and_stalled_connections_are_closed(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(gw, "READ_IDLE_S", 0.2)
    monkeypatch.setattr(gw, "LINE_DEADLINE_S", 0.5)
    loop = asyncio.get_running_loop()
    a, b = _pair()                                  # connects and says nothing
    idle = asyncio.create_task(gw.handle_conn(loop, b))
    await asyncio.wait_for(idle, 3)
    assert await _read_all(loop, a) == []           # closed quietly
    a.close()
    a, b = _pair()                                  # starts a line and never ends it
    slow = asyncio.create_task(gw.handle_conn(loop, b))
    await loop.sock_sendall(a, b'{"op": "mod')
    await asyncio.wait_for(slow, 3)
    assert (await _read_all(loop, a))[0]["error"] == "request_timeout"
    a.close()


async def test_a_reply_nobody_reads_is_dropped(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(gw, "SEND_TIMEOUT_S", 0.3)
    loop = asyncio.get_running_loop()
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    b.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    with pytest.raises(ConnectionError):            # 8 MB into a socket nobody reads
        await gw._send(loop, b, {"type": "broker_result", "result": "z" * (8 << 20)})
    a.close()
    b.close()


async def test_refused_requests_end_the_connection(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(gw, "MAX_REFUSALS_PER_CONN", 4)
    loop = asyncio.get_running_loop()
    a, b = _pair()
    server = asyncio.create_task(gw.handle_conn(loop, b))
    await loop.sock_sendall(a, b'{"op": "nope"}\n' * 12)
    await asyncio.wait_for(server, 5)
    events = await _read_all(loop, a)
    a.close()
    assert events[-1]["error"] == "too_many_refusals"
    assert sum(1 for e in events if e["error"] == "unknown_op") == 5
    # and not one DB row per refusal: the log has a rate limit too
    assert 1 <= len(await _rows("SELECT id FROM gateway_refusals")) <= 8


async def test_refusal_rows_are_rate_limited_per_box(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(gateway_log, "ROWS_BURST", 5)
    for _ in range(50):
        await gateway_log.record_refusal("x", "unknown_op", "box-a", None)
    await gateway_log.record_refusal("x", "unknown_op", "box-b", None)
    rows = await _rows("SELECT box_id FROM gateway_refusals")
    assert sum(r["box_id"] == "box-a" for r in rows) <= 6     # burst, plus a refill tick
    assert sum(r["box_id"] == "box-b" for r in rows) == 1


async def test_a_cap_trip_raises_one_event_that_counts_its_repeats(tmp_env):
    await init_db()
    for _ in range(3):
        await gateway_log.record_cap_trip("request_too_large", "9 MB request line", "box-a", None)
    ev = await _rows("SELECT kind, count FROM security_events")
    assert len(ev) == 1 and ev[0]["kind"] == "gateway_cap"      # throttled: one row, one raise


async def test_guest_package_requests_are_rate_limited(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(gw, "PACKAGE_PER_MIN", 2)
    monkeypatch.setattr(gw, "_package_for", lambda box: b"tar")
    loop = asyncio.get_running_loop()
    a, b = _pair()
    server = asyncio.create_task(gw.handle_conn(loop, b, peer_cid=9))
    await loop.sock_sendall(a, b'{"op": "get_guest_package"}\n' * 3)
    await asyncio.wait_for(server, 5)
    events = await _read_all(loop, a)
    a.close()
    assert [e.get("error") or e["type"] for e in events] == \
        ["guest_package", "guest_package", "rate_limited"]
