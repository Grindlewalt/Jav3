"""e2e BUG-3: svcd RPC and tunnels must work under uvloop (uvicorn's default
loop), where asyncio.open_connection(sock=<AF_VSOCK>) fails with Errno 92.
They use SockStream (loop.sock_*) instead; no box path may wrap a raw box
socket in an asyncio stream."""
import asyncio
import json
import re
import socket
import threading

import pytest

from backend.config import settings
from backend.vm import services

uvloop = pytest.importorskip("uvloop")


class _Transport:
    def __init__(self, sock):
        self.sock = sock

    async def connect(self, port):
        return self.sock


class _Box:
    id = "s-x"

    def __init__(self, sock):
        self.transport = _Transport(sock)


def _fake_svcd(peer: socket.socket, replies: list[bytes]):
    def run():
        f = peer.makefile("rwb")
        f.readline()
        for r in replies:
            f.write(r)
            f.flush()
        f.close()
        peer.close()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


@pytest.mark.parametrize("loop_factory", ["uvloop", "asyncio"])
def test_svcd_rpc_and_tunnel_on_both_loops(loop_factory):
    async def main():
        a, b = socket.socketpair()
        _fake_svcd(b, [json.dumps({"ok": True, "state": "running"}).encode() + b"\n"])
        out = await services.svcd_rpc(_Box(a), {"op": "status"}, timeout=5)
        assert out == {"ok": True, "state": "running"}

        a, b = socket.socketpair()
        _fake_svcd(b, [b'{"ok": true}\n', b"hello from the service"])
        r, w = await services.open_tunnel(_Box(a), 8080)
        data = b""
        while chunk := await r.read(1024):
            data += chunk
        assert data == b"hello from the service"
        w.write(b"x")
        w.close()

    if loop_factory == "uvloop":
        uvloop.run(main())
    else:
        asyncio.run(main())


def test_no_stream_wrapping_of_box_sockets():
    src = (settings.base_dir / "backend" / "vm" / "services.py").read_text()
    assert not re.search(r"open_connection\(\s*sock=\w", src)
