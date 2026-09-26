"""WP1: the gateway knows WHO is calling (peer CID / per-box listener) and
gates ops by box kind. Driven over an AF_UNIX socketpair through the existing
handle_conn seam; no vsock, no VM."""
import asyncio
import base64
import io
import json
import socket
import tarfile

import pytest

from backend.config import settings
from backend.vm import boxes, broker, gateway_server
from backend.vm.gateway_server import handle_conn


async def _rt(req: dict, **kw) -> dict:
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(handle_conn(loop, b, **kw))
    try:
        await loop.sock_sendall(a, (json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = await asyncio.wait_for(loop.sock_recv(a, 1 << 20), timeout=10)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])
    finally:
        a.close()
        await asyncio.wait_for(task, timeout=5)


def rt(req, **kw):
    return asyncio.run(_rt(req, **kw))


@pytest.fixture
def on(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_max_boxes", 8)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 3)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 10**6)
    boxes.registry.reset()
    yield
    boxes.registry.reset()


def _turn(op_id="op-1", slug="alpha"):
    broker.register_token(op_id, "tok")
    broker.register_turn(broker.TurnEnvelope(op_id=op_id, active_project=slug))


def _untar(tar_b64):
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(tar_b64)), mode="r:gz") as t:
        names = t.getnames()
        bj = json.loads(t.extractfile("box.json").read()) if "box.json" in names else None
    return names, bj


# --- flag off: nothing gated, package unchanged ----------------------------

def test_flag_off_no_gating_and_no_box_json(tmp_env):
    boxes.registry.reset()
    assert settings.vm_boxes_enabled is False
    assert rt({"op": "ping"}, peer_cid=50) == {"type": "pong"}
    r = rt({"op": "get_guest_package"}, peer_cid=50)
    names, bj = _untar(r["tar_b64"])
    assert bj is None and "backend/server.py" in names
    # a model_call from any CID reaches today's op_id check (not a kind gate)
    r = rt({"op": "model_call", "op_id": "nope", "op_token": "x"}, peer_cid=50)
    assert r["error"] == "unknown_op_id"


# --- flag on: kind gates ----------------------------------------------------

@pytest.mark.parametrize("op", ["model_call", "tool_broker_call", "taint_note"])
def test_service_box_cannot_use_turn_ops(on, op):
    svc = boxes.allocate("service", project="alpha")
    _turn()
    r = rt({"op": op, "op_id": "op-1", "op_token": "tok", "name": "x", "args": {}},
           peer_cid=svc.cid)
    assert r["type"] == "error" and r["error"] == "op_not_allowed", r
    assert "service" in r["message"]


@pytest.mark.parametrize("op", ["model_call", "tool_broker_call", "taint_note"])
def test_builder_box_cannot_use_turn_ops(on, op):
    b = boxes.allocate("builder", variant="dev")
    _turn()
    r = rt({"op": op, "op_id": "op-1", "op_token": "tok"}, peer_cid=b.cid)
    assert r["error"] == "op_not_allowed"


def test_unknown_cid_may_only_ping(on):
    assert rt({"op": "ping"}, peer_cid=77) == {"type": "pong"}
    for op in ("get_guest_package", "model_call", "taint_note", "svc_report"):
        r = rt({"op": op}, peer_cid=77)
        assert r["error"] == "op_not_allowed" and "unknown" in r["message"]


def test_service_box_package_needs_a_builder(on):
    svc = boxes.allocate("service", project="alpha")
    r = rt({"op": "get_guest_package"}, peer_cid=svc.cid)
    assert r["error"] == "no_package"


def test_registered_service_package_gets_its_box_json(on, monkeypatch):
    svc = boxes.allocate("service", project="alpha")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"print('svcd')\n"
        ti = tarfile.TarInfo("svcd.py")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
        forged = b'{"kind": "project"}'
        ti = tarfile.TarInfo("box.json")          # a builder cannot choose identity
        ti.size = len(forged)
        t.addfile(ti, io.BytesIO(forged))
    monkeypatch.setitem(gateway_server._PACKAGE_BUILDERS, "service",
                        lambda box: buf.getvalue())
    r = rt({"op": "get_guest_package"}, peer_cid=svc.cid)
    names, bj = _untar(r["tar_b64"])
    assert names.count("box.json") == 1 and "svcd.py" in names
    assert bj["kind"] == "service" and bj["net"]["guest_ip"] == "10.201.50.2"


def test_project_box_package_is_turn_package_with_box_json(on):
    p = boxes.allocate("project", project="alpha")
    r = rt({"op": "get_guest_package"}, peer_cid=p.cid)
    names, bj = _untar(r["tar_b64"])
    assert "backend/server.py" in names and "backend/boxinfo.py" in names
    assert bj["id"] == "p-alpha" and bj["net"]["gateway"] == "10.201.10.1"
    r = rt({"op": "get_guest_package"}, peer_cid=3)
    assert _untar(r["tar_b64"])[1]["kind"] == "shared"


def test_op_bound_to_another_box_is_refused(on):
    a = boxes.allocate("project", project="alpha")
    b = boxes.allocate("project", project="beta")
    _turn("op-a", "alpha")
    boxes.bind_op("op-a", a)
    for op in ("model_call", "tool_broker_call", "taint_note"):
        r = rt({"op": op, "op_id": "op-a", "op_token": "tok", "name": "x"}, peer_cid=b.cid)
        assert r["error"] == "unknown_op_id", (op, r)


def test_unix_listener_identity(on, monkeypatch):
    monkeypatch.setattr(settings, "docker_enabled", True)
    d = boxes.allocate("service", project="alpha", runtime="docker")
    _turn()
    r = rt({"op": "model_call", "op_id": "op-1", "op_token": "tok"}, box=d)
    assert r["error"] == "op_not_allowed"
    assert rt({"op": "ping"}, box=d) == {"type": "pong"}


def test_report_op_dispatch(on, monkeypatch):
    svc = boxes.allocate("service", project="alpha")
    seen = []

    async def h(loop, conn, req, box):
        seen.append(box.id)
        await gateway_server._send(loop, conn, {"type": "ok"})
    monkeypatch.setitem(gateway_server._OP_HANDLERS, "svc_report", h)
    assert rt({"op": "svc_report"}, peer_cid=svc.cid) == {"type": "ok"}
    assert seen == ["s-alpha"]
    p = boxes.allocate("project", project="alpha")
    assert rt({"op": "svc_report"}, peer_cid=p.cid)["error"] == "op_not_allowed"


# --- taint_note ----------------------------------------------------------------

def test_taint_note_taints_and_locks_persist_first(on, monkeypatch):
    p = boxes.allocate("project", project="alpha")
    _turn("op-t", "alpha")
    boxes.bind_op("op-t", p)
    order = []

    async def on_taint(slug):
        order.append(("persist", slug, "op-t" in broker._tainted))
    from backend.vm import persist
    monkeypatch.setattr(persist, "on_taint", on_taint)
    r = rt({"op": "taint_note", "op_id": "op-t", "op_token": "tok",
            "source": "screenshot:url"}, peer_cid=p.cid)
    assert r == {"type": "taint_noted", "tainted": True, "newly": True}
    assert "op-t" in broker._tainted and order == [("persist", "alpha", True)]
    r = rt({"op": "taint_note", "op_id": "op-t", "op_token": "tok"}, peer_cid=p.cid)
    assert r["newly"] is False and len(order) == 1
    broker._tainted.discard("op-t")


def test_taint_note_needs_the_token(on):
    _turn("op-u", "alpha")
    r = rt({"op": "taint_note", "op_id": "op-u", "op_token": "wrong"}, peer_cid=3)
    assert r["error"] == "unknown_op_id" and "op-u" not in broker._tainted
