"""Stopping a turn stops what it delegated (ROBUST-05). A brokered spawn_agent /
deploy_agents / research (or a model stream) runs in a task of the gateway, not of
the turn, so a stop used to cancel none of it: the child kept spending tokens and
staging writes. The gateway now registers each such task under the op_id that
asked; releasing the turn's token, a stop naming its conversation, or the guest
hanging up cancels it, and the ops it delegated to with it. Offline: AF_UNIX
socketpairs and scripted tools."""
import asyncio
import json
import socket

import pytest

from backend import chat, permissions
from backend.agent import budget as bmod
from backend.agent.budget import Budget
from backend.agent.model import Model, model
from backend.agent.tools import registry
from backend.vm import broker, gateway_server as gw


@pytest.fixture(autouse=True)
def nothing_leaks():
    yield
    assert not broker._envelopes and not broker._inflight and not broker._op_tokens


@pytest.fixture
def slow_tool(monkeypatch):
    """registry.dispatch is a 3 s tool that records how it ended."""
    ev = []

    async def long_tool(name, args):
        ev.append(f"start:{name}")
        try:
            await asyncio.sleep(3)
            ev.append(f"finished:{name}")
        except asyncio.CancelledError:
            ev.append(f"cancelled:{name}")
            raise
        return "report"

    async def no_gate(name, args):
        return None
    monkeypatch.setattr(registry, "dispatch", long_tool)
    monkeypatch.setattr(permissions, "gate", no_gate)
    return ev


def _turn(op_id, conv, parent=None):
    broker.register_turn(broker.TurnEnvelope(op_id=op_id, conversation_id=conv,
                                             parent_op=parent))
    broker.register_token(op_id, "tok")


def _done(*op_ids):
    for o in op_ids:
        broker.release_token(o)
        broker.release_turn(o)


async def _call(loop, op_id, name="spawn_agent"):
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    handler = asyncio.create_task(gw.handle_conn(loop, b))
    req = {"op": "tool_broker_call", "op_id": op_id, "op_token": "tok",
           "name": name, "args": {}}
    await loop.sock_sendall(a, (json.dumps(req) + "\n").encode())
    await asyncio.sleep(0.2)                       # the tool is running
    return a, handler


async def _reply(loop, a):
    data = await asyncio.wait_for(loop.sock_recv(a, 65536), 3)
    return json.loads(data.split(b"\n", 1)[0]) if data else None


async def test_releasing_the_turn_cancels_its_brokered_call(slow_tool):
    """The stop path: guest_turn's finally releases the token (the hunter's repro,
    which used to end with 'finished (tokens spent, files written)')."""
    _turn("guest:900", 900)
    loop = asyncio.get_running_loop()
    a, handler = await _call(loop, "guest:900")
    _done("guest:900")
    reply = await _reply(loop, a)
    a.close()                                      # the connection stays open until the guest leaves
    await asyncio.wait_for(handler, 3)
    assert slow_tool == ["start:spawn_agent", "cancelled:spawn_agent"]
    assert reply["error"] == "turn_stopped"


async def test_the_guest_hanging_up_cancels_the_call(slow_tool):
    _turn("guest:901", 901)
    loop = asyncio.get_running_loop()
    a, handler = await _call(loop, "guest:901")
    a.close()                                      # the guest process went away
    await asyncio.wait_for(handler, 3)
    _done("guest:901")
    assert slow_tool == ["start:spawn_agent", "cancelled:spawn_agent"]


async def test_a_call_that_finishes_is_untouched(monkeypatch):
    async def quick(name, args):
        return "ok"

    async def no_gate(name, args):
        return None
    monkeypatch.setattr(registry, "dispatch", quick)
    monkeypatch.setattr(permissions, "gate", no_gate)
    _turn("guest:902", 902)
    loop = asyncio.get_running_loop()
    a, handler = await _call(loop, "guest:902", "read_file")
    reply = await _reply(loop, a)
    a.close()
    await asyncio.wait_for(handler, 3)
    _done("guest:902")
    assert reply["type"] == "broker_result" and reply["result"] == "ok"
    assert not broker._inflight


async def test_stopping_the_parent_cancels_the_children_it_started(slow_tool):
    """A funnel node is its own op whose parent_op is the deploy_agents turn."""
    _turn("guest:910", 910)
    _turn("guest:911", 911, parent="guest:910")
    _turn("guest:912", 912, parent="guest:911")            # a grandchild
    loop = asyncio.get_running_loop()
    a1, h1 = await _call(loop, "guest:911", "web_read")
    a2, h2 = await _call(loop, "guest:912", "research")
    n = broker.cancel_inflight("guest:910")
    assert n == 2
    for a, h in ((a1, h1), (a2, h2)):
        assert (await _reply(loop, a))["error"] == "turn_stopped"
        a.close()
        await asyncio.wait_for(h, 3)
    _done("guest:910", "guest:911", "guest:912")
    assert sorted(slow_tool) == ["cancelled:research", "cancelled:web_read",
                                 "start:research", "start:web_read"]


async def test_the_stop_endpoint_does_not_wait_for_the_turns_teardown(slow_tool):
    """chat._stop cancels the brokered work at once; the turn's finally (which
    awaits the workspace sweep and can be slow or wedged) has not released the
    token yet."""
    _turn("guest:920", 920)
    loop = asyncio.get_running_loop()
    a, handler = await _call(loop, "guest:920")

    async def turn():
        await asyncio.sleep(30)
    chat._active_turns[920] = asyncio.create_task(turn())
    try:
        assert chat._stop(920) is True
        assert (await _reply(loop, a))["error"] == "turn_stopped"
        a.close()
        await asyncio.wait_for(handler, 3)
    finally:
        chat._active_turns.pop(920, None)
        _done("guest:920")
    assert slow_tool[-1] == "cancelled:spawn_agent"


async def test_stop_project_reaches_the_conversations_beneath(slow_tool):
    _turn("guest:930", 930)
    _turn("guest:931", 931, parent="guest:930")
    _turn("guest:940", 940)                                # another project's turn
    loop = asyncio.get_running_loop()
    a1, h1 = await _call(loop, "guest:931", "web_read")
    a2, h2 = await _call(loop, "guest:940", "web_read")
    # the bulk stop of a project names its tree; the child has no turn task of its own
    chat._bulk_stop(([], [], []), False, tree={930, 931})
    assert (await _reply(loop, a1))["error"] == "turn_stopped"
    a1.close()
    await asyncio.wait_for(h1, 3)
    assert not h2.done()                                   # untouched
    assert broker.cancel_conversations(None) == 1          # the operator's stop-all
    assert (await _reply(loop, a2))["error"] == "turn_stopped"
    a2.close()
    await asyncio.wait_for(h2, 3)
    _done("guest:930", "guest:931", "guest:940")


async def test_a_model_stream_is_cancelled_with_its_turn(monkeypatch):
    started = asyncio.Event()

    async def slow_stream(self, base, key, payload):
        started.set()
        await asyncio.sleep(3)
        yield {"type": "raw", "content": "late", "tool_calls": [], "usage": None}
    monkeypatch.setattr(Model, "_stream_once", slow_stream)
    monkeypatch.setattr(model, "api_key", "sk-secret")
    monkeypatch.setattr(model.transport, "api_key", "sk-secret")
    bmod.register("guest:950", Budget(10**9, 10**9))
    _turn("guest:950", 950)
    loop = asyncio.get_running_loop()
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    handler = asyncio.create_task(gw.handle_conn(loop, b))
    req = {"op": "model_call", "op_id": "guest:950", "op_token": "tok",
           "messages": [{"role": "user", "content": "hi"}]}
    await loop.sock_sendall(a, (json.dumps(req) + "\n").encode())
    await asyncio.wait_for(started.wait(), 3)
    broker.release_token("guest:950")
    assert (await _reply(loop, a))["error"] == "turn_stopped"
    a.close()
    await asyncio.wait_for(handler, 3)
    bmod.release("guest:950")
    broker.release_turn("guest:950")


async def test_a_pipelined_request_is_kept_while_one_is_served(monkeypatch):
    """The watcher reads the socket while a call runs; a second request sent
    meanwhile must not be lost."""
    async def quick(name, args):
        await asyncio.sleep(0.3)
        return name

    async def no_gate(name, args):
        return None
    monkeypatch.setattr(registry, "dispatch", quick)
    monkeypatch.setattr(permissions, "gate", no_gate)
    _turn("guest:960", 960)
    loop = asyncio.get_running_loop()
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    handler = asyncio.create_task(gw.handle_conn(loop, b))

    def line(name):
        return (json.dumps({"op": "tool_broker_call", "op_id": "guest:960",
                            "op_token": "tok", "name": name, "args": {}}) + "\n").encode()
    await loop.sock_sendall(a, line("first"))
    await asyncio.sleep(0.1)
    await loop.sock_sendall(a, line("second"))
    got = b""
    while got.count(b"\n") < 2:
        got += await asyncio.wait_for(loop.sock_recv(a, 65536), 3)
    a.close()
    await asyncio.wait_for(handler, 3)
    _done("guest:960")
    assert [json.loads(x)["result"] for x in got.splitlines()] == ["first", "second"]
