"""Host broker for the operator co-working shell INTO the guest.

Two front doors, one back door. The back door is a vsock connection to the
guest PTY server (guest/backend/shell.py). The front doors are:

  - a WebSocket (/api/guest/shell) the browser terminal panel speaks, and
  - a Unix socket (data/vm/guest-shell.sock) the `guest-shell` CLI speaks,

both relaying the same newline-delimited JSON frame protocol. The broker is the
ONLY path in: it authenticates, pins the guest for the session (so the idle
reaper can't scrub it out from under a live shell), optionally primes the active
project's files into the guest so the operator lands beside the agent's tools,
then relays frames until either side hangs up and the pin is released.

Containment is unchanged: the guest still has no NIC, no secrets; a shell here
is a seat in the same disposable box the agent's code runs in, reached only
through the host supervisor.
"""
import asyncio
import json
import socket

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import COOKIE_NAME, user_from_token
from .config import settings

router = APIRouter(tags=["guest-shell"])


class _LineSock:
    """Newline-framed async I/O over a raw vsock socket. asyncio streams can't
    wrap AF_VSOCK (uvloop's transport pokes TCP-only sockopts -> ENOPROTOOPT),
    so the whole codebase uses loop.sock_* directly; this buffers lines on top."""
    def __init__(self, sock: socket.socket):
        self._s = sock
        self._buf = b""

    async def readline(self) -> bytes:
        while b"\n" not in self._buf:
            chunk = await asyncio.get_running_loop().sock_recv(self._s, 65536)
            if not chunk:
                line, self._buf = self._buf, b""
                return line                          # EOF: flush any tail, then b""
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line + b"\n"

    async def write_line(self, data: bytes) -> None:
        await asyncio.get_running_loop().sock_sendall(self._s, data)

    def close(self) -> None:
        try:
            self._s.close()
        except OSError:
            pass


async def _connect_guest(port: int) -> _LineSock:
    """A line-framed vsock connection to the guest, retried briefly (the PTY
    listener comes up a beat after the run-turn one on a fresh boot). Blocking
    connect in an executor — uvloop's sock_connect chokes on an AF_VSOCK tuple."""
    loop = asyncio.get_running_loop()
    last = None
    for _ in range(20):
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        try:
            await loop.run_in_executor(
                None, s.connect, (settings.vm_guest_cid, port))
            return _LineSock(s)
        except OSError as e:
            last = e
            s.close()
            await asyncio.sleep(0.5)
    raise ConnectionError(f"guest shell unreachable on vsock :{port}: {last}")


async def _prime_project(slug: str) -> None:
    """Push the project's workspace into the guest (reusing the run-turn
    server's `prime` mode) so the shell sees the same files the file tools do.
    Best-effort: a shell still opens if this fails."""
    from .vm.guest_turn import GUEST_RUNTURN_PORT
    from .vm import workspace_xfer
    import base64
    try:
        tar_b64 = base64.b64encode(workspace_xfer.build_merged_tar(slug)).decode()
        conn = await _connect_guest(GUEST_RUNTURN_PORT)
    except (ConnectionError, OSError):
        return
    try:
        await conn.write_line((json.dumps(
            {"mode": "prime", "active_slug": slug,
             "workspace_tar_b64": tar_b64}) + "\n").encode())
        await asyncio.wait_for(conn.readline(), timeout=30)     # {"type":"primed"}
    except (OSError, asyncio.TimeoutError):
        pass
    finally:
        conn.close()


async def _session(client_recv, client_send, slug: str | None):
    """Pin the guest, bridge frames between a front door and the guest PTY,
    release on exit. `client_recv` awaits one JSON line (str) or None at EOF;
    `client_send` takes one JSON line (str)."""
    from .vm.lifecycle import vm, VMError
    if not settings.guest_shell_enabled:
        await client_send(json.dumps({"type": "o",
            "data": _b64("guest shell is disabled (JARVIS_GUEST_SHELL_ENABLED)\r\n")}))
        return
    try:
        await vm.acquire()                          # boots if needed + pins
    except VMError as e:
        await client_send(json.dumps({"type": "o", "data": _b64(f"{e}\r\n")}))
        return
    try:
        if slug:
            await _prime_project(slug)
        conn = await _connect_guest(settings.vm_shell_port)
    except (ConnectionError, OSError) as e:
        await client_send(json.dumps({"type": "o", "data": _b64(f"{e}\r\n")}))
        vm.release()
        return

    async def to_guest():
        while True:
            line = await client_recv()
            if line is None:
                return
            await conn.write_line((line.rstrip("\n") + "\n").encode())

    async def from_guest():
        while True:
            line = await conn.readline()
            if not line:
                return
            await client_send(line.decode(errors="replace").rstrip("\n"))

    a = asyncio.ensure_future(to_guest())
    b = asyncio.ensure_future(from_guest())
    try:
        await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (a, b):
            t.cancel()
        conn.close()
        vm.release()


def _b64(text: str) -> str:
    import base64
    return base64.b64encode(text.encode()).decode()


@router.websocket("/api/guest/shell")
async def guest_shell_ws(ws: WebSocket, slug: str | None = None):
    # WebSocket can't use Depends(require_user); validate the session cookie
    if user_from_token(ws.cookies.get(COOKIE_NAME)) is None:
        await ws.close(code=4401)
        return
    await ws.accept()

    async def recv():
        try:
            return await ws.receive_text()
        except WebSocketDisconnect:
            return None

    async def send(line: str):
        await ws.send_text(line)

    try:
        await _session(recv, send, slug)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await ws.close()
        except RuntimeError:
            pass


# --- Unix-socket front door for the `guest-shell` CLI -------------------------

_unix_server: asyncio.AbstractServer | None = None


def _sock_path():
    return settings.vm_dir / "guest-shell.sock"


async def _unix_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    async def recv():
        line = await reader.readline()
        return None if not line else line.decode(errors="replace")

    async def send(line: str):
        writer.write((line + "\n").encode())
        await writer.drain()

    # the CLI's first line is the init frame; sniff its slug for priming
    slug = None
    try:
        first = await reader.readline()
        if not first:
            return
        try:
            slug = json.loads(first).get("slug")
        except json.JSONDecodeError:
            pass

        async def recv_with_first(_first=first):
            nonlocal first
            if first is not None:
                f, first = first, None
                return f.decode(errors="replace")
            line = await reader.readline()
            return None if not line else line.decode(errors="replace")

        await _session(recv_with_first, send, slug)
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def start_unix_server() -> None:
    """Listen on the local Unix socket so an operator already on the Pi can
    `python -m backend.cli guest-shell` into the guest. Best-effort."""
    global _unix_server
    if not settings.guest_shell_enabled:
        return
    path = _sock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    try:
        _unix_server = await asyncio.start_unix_server(_unix_client, str(path))
        path.chmod(0o600)                            # operator-only
    except (OSError, NotImplementedError) as e:
        print(f"[guest-shell] unix socket disabled: {e}")


async def stop_unix_server() -> None:
    if _unix_server is not None:
        _unix_server.close()
    _sock_path().unlink(missing_ok=True)
