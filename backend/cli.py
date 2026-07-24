"""Admin CLI:
  python -m backend.cli create-user <username> [password]
  python -m backend.cli guest-shell [project-slug]   # drop into the sandbox guest
"""
import asyncio
import getpass
import sys

from .auth import hash_password
from .db import get_db, init_db


async def create_user(username: str, password: str) -> None:
    await init_db()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?) "
            "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash",
            (username, hash_password(password)),
        )
        await db.commit()
    finally:
        await db.close()
    print(f"user '{username}' created/updated")


def guest_shell(slug: str | None) -> None:
    """Co-work in the guest from a terminal (you're already SSH'd to the Pi).
    Bridges this TTY to the running app's guest-shell Unix socket, which pins
    the guest and relays the PTY. Ctrl-] detaches without killing the guest."""
    import base64
    import fcntl
    import json
    import os
    import signal
    import socket
    import struct
    import termios
    import tty
    from .config import settings

    sock_path = settings.vm_dir / "guest-shell.sock"
    if not sock_path.exists():
        print(f"no guest-shell socket at {sock_path} — is the app running with "
              "guest_shell_enabled on?", file=sys.stderr)
        sys.exit(1)
    if not sys.stdin.isatty():
        print("guest-shell needs an interactive terminal", file=sys.stderr)
        sys.exit(1)

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(sock_path))
    s.setblocking(False)

    def winsize():
        try:
            r, c, _, _ = struct.unpack(
                "HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ,
                                    b"\0" * 8))
            return r, c
        except OSError:
            return 24, 80

    loop = asyncio.new_event_loop()
    rows, cols = winsize()
    init = {"type": "init", "rows": rows, "cols": cols}
    if slug:
        init["slug"] = slug
    s.sendall((json.dumps(init) + "\n").encode())

    old = termios.tcgetattr(sys.stdin)
    tty.setraw(sys.stdin.fileno())
    inbuf = {"data": b""}

    def on_stdin():
        try:
            data = os.read(sys.stdin.fileno(), 4096)
        except OSError:
            return
        if b"\x1d" in data:                          # Ctrl-] -> detach
            loop.stop()
            return
        frame = json.dumps({"type": "i", "data": base64.b64encode(data).decode()})
        s.sendall((frame + "\n").encode())

    def on_sock():
        try:
            chunk = s.recv(65536)
        except BlockingIOError:
            return
        if not chunk:
            loop.stop()
            return
        inbuf["data"] += chunk
        while b"\n" in inbuf["data"]:
            line, inbuf["data"] = inbuf["data"].split(b"\n", 1)
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "o":
                os.write(sys.stdout.fileno(), base64.b64decode(ev.get("data") or ""))
            elif ev.get("type") == "exit":
                loop.stop()

    def on_winch():
        r, c = winsize()
        s.sendall((json.dumps({"type": "r", "rows": r, "cols": c}) + "\n").encode())

    loop.add_reader(sys.stdin.fileno(), on_stdin)
    loop.add_reader(s.fileno(), on_sock)
    try:
        loop.add_signal_handler(signal.SIGWINCH, on_winch)
    except (NotImplementedError, ValueError):
        pass
    print("[co-working in the guest — Ctrl-] to detach]\r")
    try:
        loop.run_forever()
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        s.close()
        print("\r\n[detached from guest]")


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "create-user":
        username = sys.argv[2]
        password = sys.argv[3] if len(sys.argv) > 3 else getpass.getpass("password: ")
        asyncio.run(create_user(username, password))
    elif len(sys.argv) >= 2 and sys.argv[1] == "guest-shell":
        guest_shell(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print("usage: python -m backend.cli create-user <username> [password]\n"
              "       python -m backend.cli guest-shell [project-slug]")
        sys.exit(1)


if __name__ == "__main__":
    main()
