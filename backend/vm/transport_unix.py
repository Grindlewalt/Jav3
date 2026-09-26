"""The AF_UNIX transport for docker boxes (WP8; docs/docker-runtime.md).

Same newline-delimited JSON as the vsock path: the gateway's `handle_conn`, the
guest's run-turn server and `guest_turn` see a connected stream socket and
nothing else. Layout (the contract's, docs/boxes-contract.md C):

    host view                 container view         who listens
    <box.dir>/sock/  (0700)   /run/jav3/  (rw)
        gateway.sock          gateway.sock           the HOST gateway, bound to this box
        proxy.sock            proxy.sock             the HOST egress proxy, bound to this box
        5556/5557/5558.sock   <port>.sock            the GUEST: run-turn / shell / svcd

Caller identity is the listener: each box gets its own gateway.sock and
proxy.sock, and the handler is bound to that box when the listener is made.
Nothing the guest says about itself is used.

The directory is writable by the guest (it must create its listeners), so the
host treats it as hostile:

  * the guest can unlink or shadow gateway.sock / proxy.sock. That only cuts
    itself off: the host never connects to those names, and a guest-made
    symlink resolves inside the container, not on the host;
  * the guest can swap `5556.sock` for a symlink to a HOST socket (the docker
    socket, say), which the host's connect() would follow. So `connect()`
    lstat's first (no symlink, must be a socket) and, after connecting and
    before sending a byte, checks the peer's uid (SO_PEERCRED) against the
    container's host uid. dockerd / containerd / the Jav3 service itself all
    fail that check.

This subclass keeps boxes.UnixTransport's paths and adds the checked connect
and the proxy socket; importing docker_runtime installs it for runtime docker.
"""
from __future__ import annotations

import asyncio
import os
import socket
import stat
import struct
import sys
from pathlib import Path
from typing import Awaitable, Callable

from . import boxes

GUEST_ROOT = boxes.UnixTransport.GUEST_DIR      # /run/jav3
GATEWAY_NAME = "gateway.sock"
PROXY_NAME = "proxy.sock"
# the in-container egress forwarder listens here and splices to proxy.sock
GUEST_PROXY_URL = "http://127.0.0.1:8443"


class TransportError(OSError):
    """A unix endpoint that failed the host's checks (not a socket, a symlink,
    or the wrong peer). Subclass of OSError so callers that already treat a
    failed connect as 'guest not up yet' keep doing so."""


def peer_uid(sock: socket.socket) -> int | None:
    """The uid of the process at the other end (host namespace). Linux:
    SO_PEERCRED; macOS: LOCAL_PEERCRED via getpeereid semantics. None when the
    platform offers neither (then the caller decides; docker_runtime only runs
    on Linux)."""
    if hasattr(socket, "SO_PEERCRED"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    if sys.platform == "darwin":
        LOCAL_PEERCRED = 0x001        # <sys/un.h>: struct xucred
        try:
            raw = sock.getsockopt(0, LOCAL_PEERCRED, 76)
            return struct.unpack_from("Ii", raw)[1]   # cr_version, cr_uid
        except OSError:
            return None
    return None


def _check_is_socket(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(st.st_mode):
        raise TransportError(f"{path.name}: a symlink, refused")
    if not stat.S_ISSOCK(st.st_mode):
        raise TransportError(f"{path.name}: not a socket, refused")


class UnixTransport(boxes.UnixTransport):
    """boxes.UnixTransport (same paths) plus the proxy socket and a connect
    that does not trust the guest-writable directory. The expected peer uid
    is the controller's `guest_uid` (the container's host-side uid)."""

    def proxy_path(self) -> Path:
        return self.host_dir / PROXY_NAME

    def proxy_endpoint(self) -> dict:
        return {"transport": "unix", "path": f"{GUEST_ROOT}/{PROXY_NAME}",
                "url": GUEST_PROXY_URL}

    def _expected_uid(self) -> int | None:
        ctl = self.box.ctl
        return getattr(ctl, "guest_uid", None) if ctl is not None else None

    async def connect(self, port: int) -> socket.socket:
        return await connect_checked(self.host_path(port),
                                     expected_uid=self._expected_uid())


async def connect_checked(path: Path, *, expected_uid: int | None) -> socket.socket:
    """Connect to a guest-created socket without trusting the directory:
    lstat (no symlink, must be a socket), connect, then the peer uid must be
    the container's. The lstat is advisory (the guest can race it); the peer
    check after connect is the guarantee, and it runs before any byte is sent.
    Returns a connected non-blocking socket."""
    path = Path(path)
    _check_is_socket(path)
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        await loop.run_in_executor(None, s.connect, str(path))
        uid = peer_uid(s)
        if expected_uid is not None:
            if uid is not None and uid != expected_uid:
                raise TransportError(
                    f"{path.name}: peer uid {uid} is not the box's ({expected_uid})")
        elif uid == 0 and os.getuid() != 0:
            raise TransportError(f"{path.name}: peer is root, refused")
    except BaseException:
        s.close()
        raise
    s.setblocking(False)
    return s


# --- host-side listeners -------------------------------------------------------

ConnHandler = Callable[[asyncio.AbstractEventLoop, socket.socket], Awaitable[None]]


class UnixListener:
    """One AF_UNIX listener on a host path, each accepted connection handed to
    `handler(loop, conn)` (the gateway's handle_conn shape). The socket file is
    created fresh (a stale file is unlinked first, a non-socket refused) and
    chmod'ed to `mode`; access control is the directory's (0700 + ACL)."""

    def __init__(self, path: Path, handler: ConnHandler, *, mode: int = 0o666):
        self.path = Path(path)
        self.handler = handler
        self.mode = mode
        self.accepted = 0
        self._sock: socket.socket | None = None
        self._task: asyncio.Task | None = None
        self._conns: set[asyncio.Task] = set()

    async def start(self) -> None:
        if self.path.is_symlink() or (self.path.exists()
                                      and not stat.S_ISSOCK(os.lstat(self.path).st_mode)):
            raise TransportError(f"{self.path}: exists and is not a socket")
        self.path.unlink(missing_ok=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(str(self.path))
            os.chmod(self.path, self.mode)
            s.listen(16)
            s.setblocking(False)
        except BaseException:
            s.close()
            raise
        self._sock = s
        self._task = asyncio.create_task(self._serve())

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                conn, _ = await loop.sock_accept(self._sock)
            except asyncio.CancelledError:
                raise
            except OSError:
                await asyncio.sleep(0.2)
                continue
            conn.setblocking(False)
            self.accepted += 1
            t = asyncio.create_task(self.handler(loop, conn))
            self._conns.add(t)
            t.add_done_callback(self._conns.discard)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        for t in list(self._conns):
            t.cancel()
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        try:
            if self.path.is_socket():
                self.path.unlink()
        except OSError:
            pass


def stream_handler(fn: Callable[[asyncio.StreamReader, asyncio.StreamWriter],
                                      Awaitable[None]]) -> ConnHandler:
    """Adapt a (reader, writer) handler (the egress proxy's shape) to the
    (loop, conn) shape UnixListener hands out."""
    async def h(loop, conn):
        conn.setblocking(False)
        reader, writer = await asyncio.open_unix_connection(sock=conn)
        try:
            await fn(reader, writer)
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
    return h


async def splice(r1: asyncio.StreamReader, w1: asyncio.StreamWriter,
                 r2: asyncio.StreamReader, w2: asyncio.StreamWriter) -> None:
    """Copy both directions until either side closes."""
    async def pump(r, w):
        try:
            while True:
                b = await r.read(65536)
                if not b:
                    break
                w.write(b)
                await w.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
    await asyncio.gather(pump(r1, w2), pump(r2, w1))
