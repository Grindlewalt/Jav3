"""M2: the guest's model shim passes the gateway's retry event on to the loop and
reports a gateway that closed the connection mid-reply instead of ending the
stream with no message (which the loop met as an AssertionError)."""
import asyncio
import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest

GUEST = Path(__file__).resolve().parents[1] / "guest" / "backend"


@pytest.fixture
def shim(monkeypatch):
    """guest/backend loaded as its own package, beside the host's `backend`."""
    spec = importlib.util.spec_from_file_location(
        "gbackend", GUEST / "__init__.py", submodule_search_locations=[str(GUEST)])
    pkg = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(socket, "VMADDR_CID_HOST", 2, raising=False)   # not on a Mac
    monkeypatch.setitem(sys.modules, "gbackend", pkg)
    spec.loader.exec_module(pkg)
    model = importlib.import_module("gbackend.agent.model")
    boxinfo = importlib.import_module("gbackend.boxinfo")
    for name in [n for n in sys.modules if n.startswith("gbackend")]:
        monkeypatch.setitem(sys.modules, name, sys.modules[name])   # removed at teardown
    return model, boxinfo


async def _run(shim, monkeypatch, lines):
    """What the shim yields for a gateway that answers `lines`, then closes."""
    model, boxinfo = shim
    a, b = socket.socketpair()
    b.setblocking(False)
    monkeypatch.setattr(boxinfo, "unix_gateway", lambda: True)
    monkeypatch.setattr(boxinfo, "gateway_connect", lambda: a)
    loop = asyncio.get_running_loop()

    async def gateway():
        await loop.sock_recv(b, 1 << 20)             # the request
        for ln in lines:
            await loop.sock_sendall(b, (json.dumps(ln) + "\n").encode())
        b.close()

    task = asyncio.create_task(gateway())
    out = []
    try:
        async for ev in model.model.complete([{"role": "user", "content": "x"}]):
            out.append(ev["type"])
    except Exception as e:  # noqa: BLE001
        return out, e
    finally:
        await task
    return out, None


async def test_retry_event_reaches_the_loop(shim, monkeypatch):
    out, err = await _run(shim, monkeypatch, [
        {"type": "token", "text": "a"}, {"type": "retry", "reason": "x"},
        {"type": "token", "text": "ab"},
        {"type": "message", "content": "ab", "tool_calls": [], "usage": None}])
    assert err is None and out == ["token", "retry", "token", "message"]


async def test_a_connection_closed_mid_reply_is_an_error(shim, monkeypatch):
    model, _ = shim
    out, err = await _run(shim, monkeypatch, [{"type": "token", "text": "a"}])
    assert out == ["token"] and isinstance(err, model.ModelError)
    assert "closed the connection" in str(err)
