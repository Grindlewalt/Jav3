"""Guest co-working PTY server — the operator's shell INSIDE the sandbox.

A second vsock listener (settings.vm_shell_port) alongside the run-turn server.
The host broker dials it; this forks a login bash in a PTY and bridges bytes as
newline-delimited JSON frames (base64 payloads, so a terminal's control bytes
and newlines survive the line framing):

    client -> guest : {"type":"init","cols":C,"rows":R,"slug":S}   (first frame)
                      {"type":"i","data":<b64 stdin>}
                      {"type":"r","cols":C,"rows":R}                (window resize)
    guest -> client : {"type":"o","data":<b64 stdout>}
                      {"type":"exit","code":N}

The shell has the guest's whole (disposable, secret-free) filesystem. It cwds
into the pushed project workspace when the init frame names one that exists, so
the operator lands where the agent's file tools operate. Nothing here reconciles
back to the host — this is a live exploration/debug seat in the agent's sandbox,
not a write path; durable edits still go through the file tools / editor panel.
"""
import asyncio
import base64
import fcntl
import json
import os
import pty
import signal
import socket
import struct
import termios

from . import config as guest_config

PORT = 5557


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _spawn(slug: str | None, rows: int, cols: int) -> tuple[int, int]:
    """Fork bash under a new PTY. Returns (pid, master_fd)."""
    pid, master = pty.fork()
    if pid == 0:                                    # child -> exec bash, never returns
        cwd = None
        if slug:
            p = guest_config.settings.projects_dir / slug
            if p.is_dir():
                cwd = str(p)
        if cwd is None:
            proj = guest_config.settings.projects_dir
            cwd = str(proj) if proj.is_dir() else "/root"
        try:
            os.chdir(cwd)
        except OSError:
            pass
        os.environ.update(TERM="xterm-256color", HOME="/root",
                          PS1=r"[guest \W]$ ")
        os.execvp("/bin/bash", ["/bin/bash", "-l"])
        os._exit(127)                               # exec failed
    _set_winsize(master, rows, cols)
    return pid, master


async def _handle(loop, conn) -> None:
    conn.setblocking(False)
    pid = master = None
    try:
        # first frame: init (must arrive before we fork)
        buf = b""
        while b"\n" not in buf:
            chunk = await loop.sock_recv(conn, 65536)
            if not chunk:
                return
            buf += chunk
        line, rest = buf.split(b"\n", 1)
        init = json.loads(line)
        if init.get("type") != "init":
            return
        rows = int(init.get("rows") or 24)
        cols = int(init.get("cols") or 80)
        pid, master = _spawn(init.get("slug"), rows, cols)
        os.set_blocking(master, False)

        async def send(ev: dict) -> None:
            try:
                await loop.sock_sendall(conn, (json.dumps(ev) + "\n").encode())
            except OSError:
                raise ConnectionError

        # pty master -> client, driven by readiness (no busy-loop, no thread)
        out_q: asyncio.Queue = asyncio.Queue()

        def _on_readable() -> None:
            try:
                data = os.read(master, 65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                data = b""                          # shell exited / pty closed
            out_q.put_nowait(data)
            if not data:
                loop.remove_reader(master)

        loop.add_reader(master, _on_readable)

        async def pump_out() -> None:
            while True:
                data = await out_q.get()
                if not data:                        # EOF: shell exited
                    return
                await send({"type": "o",
                            "data": base64.b64encode(data).decode()})

        # client frames -> pty master (input / resize)
        async def pump_in() -> None:
            pending = rest
            while True:
                while b"\n" not in pending:
                    chunk = await loop.sock_recv(conn, 65536)
                    if not chunk:
                        return
                    pending += chunk
                frame, pending = pending.split(b"\n", 1)
                if not frame.strip():
                    continue
                try:
                    ev = json.loads(frame)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t == "i":
                    os.write(master, base64.b64decode(ev.get("data") or ""))
                elif t == "r":
                    _set_winsize(master, int(ev.get("rows") or rows),
                                 int(ev.get("cols") or cols))

        out_task = asyncio.ensure_future(pump_out())
        in_task = asyncio.ensure_future(pump_in())
        try:
            _, pending_tasks = await asyncio.wait(
                {out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending_tasks:
                t.cancel()
        finally:
            try:
                loop.remove_reader(master)
            except (OSError, ValueError):
                pass
        code = 0
        try:
            _, status = os.waitpid(pid, os.WNOHANG)
            code = os.waitstatus_to_exitcode(status) if status else 0
        except ChildProcessError:
            pass
        try:
            await send({"type": "exit", "code": code})
        except ConnectionError:
            pass
    except (ConnectionError, OSError, json.JSONDecodeError):
        pass
    finally:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError, OSError):
                pass
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        try:
            conn.close()
        except OSError:
            pass


async def serve() -> None:
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((socket.VMADDR_CID_ANY, PORT))
    s.listen(4)
    s.setblocking(False)
    print(f"GUEST-SHELL-SERVER: listening on vsock :{PORT}", flush=True)
    while True:
        conn, _ = await loop.sock_accept(s)
        asyncio.ensure_future(_handle(loop, conn))
