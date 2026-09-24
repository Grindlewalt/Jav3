"""Admin CLI:
  python -m backend.cli create-user <username> [password]  # omit it: hidden prompt
  python -m backend.cli guest-shell [project-slug]   # drop into the sandbox guest
  python -m backend.cli services-check               # probe the companion services
  python -m backend.cli paths [name]                 # resolved state paths
  python -m backend.cli migrate-state [--to DIR]     # move checkout state to the state dir
  python -m backend.cli backup [--if-configured]     # rclone the state to the remote
  python -m backend.cli restore [REMOTE] [--to DIR] [--secrets|--no-secrets] [--force]
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


def services_check() -> None:
    """Which companion services (derived from JARVIS_SERVICES_HOST unless set
    explicitly) answer a 1s TCP connect from here."""
    from .config import settings
    from .lan import probe
    print(f"services_host: {settings.services_host}")
    for r in asyncio.run(probe()):
        print(f"  {'ok  ' if r['reachable'] else 'DOWN'}  {r['service']:<14} {r['url']}")


def _prompt_password() -> str:
    pw = getpass.getpass("password: ")
    if not pw:
        sys.exit("empty password refused")
    if getpass.getpass("again: ") != pw:
        sys.exit("passwords did not match")
    return pw


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "create-user":
        username = sys.argv[2]
        # argv is kept for scripts, but it lands in shell history and `ps`;
        # interactively, leave it off and type it at the hidden prompt
        password = sys.argv[3] if len(sys.argv) > 3 else _prompt_password()
        asyncio.run(create_user(username, password))
    elif len(sys.argv) >= 2 and sys.argv[1] == "guest-shell":
        guest_shell(sys.argv[2] if len(sys.argv) > 2 else None)
    elif len(sys.argv) >= 2 and sys.argv[1] == "services-check":
        services_check()
    elif len(sys.argv) >= 2 and sys.argv[1] == "paths":
        paths(sys.argv[2] if len(sys.argv) > 2 else None)
    elif len(sys.argv) >= 2 and sys.argv[1] in ("migrate-state", "backup", "restore"):
        state_command(sys.argv[1], sys.argv[2:])
    else:
        print(__doc__.split("\n", 1)[1].rstrip())
        sys.exit(1)


_PATHS = ("state_dir", "data_dir", "db_path", "memory_dir", "projects_dir",
          "skills_dir", "agents_dir", "tools_dir", "vm_dir")


def paths(name: str | None) -> None:
    """One path (for scripts: `VM_DIR=$(python -m backend.cli paths vm_dir)`)
    or all of them, resolved exactly as the service resolves them."""
    from .config import settings
    if name:
        if name not in _PATHS:
            print(f"unknown path '{name}' (one of: {', '.join(_PATHS)})", file=sys.stderr)
            sys.exit(1)
        print(getattr(settings, name))
        return
    for n in _PATHS:
        print(f"{n:13} {getattr(settings, n)}")
    if settings.legacy_layout:
        print("\nnote: running from the legacy in-checkout layout; "
              "run `python -m backend.cli migrate-state`")


def state_command(cmd: str, args: list[str]) -> None:
    import argparse
    from pathlib import Path
    from . import backup
    from .statemigrate import MigrateError, migrate_state

    ap = argparse.ArgumentParser(prog=f"python -m backend.cli {cmd}")
    if cmd == "migrate-state":
        ap.add_argument("--to", type=Path)
    elif cmd == "backup":
        ap.add_argument("--if-configured", action="store_true",
                        help="exit 0 quietly when no remote is set (the timer)")
    else:
        ap.add_argument("remote", nargs="?")
        ap.add_argument("--to", type=Path)
        ap.add_argument("--secrets", action=argparse.BooleanOptionalAction,
                        default=None)
        ap.add_argument("--force", action="store_true")
    a = ap.parse_args(args)
    try:
        if cmd == "migrate-state":
            lines = migrate_state(a.to)
        elif cmd == "backup":
            if a.if_configured and not backup.valid_remote(backup.load_config()["remote"]):
                print("backups not configured (no remote) — skipping")
                return
            st = backup.run_backup()
            lines = [f"backed up {st['bytes']:,} bytes to {st['remote']} "
                     f"in {st['seconds']}s"]
        else:
            lines = backup.restore(a.remote, a.to, a.secrets, a.force)
    except (MigrateError, backup.BackupError) as e:
        print(f"{cmd}: {e}", file=sys.stderr)
        sys.exit(1)
    for line in lines:
        print(line)


if __name__ == "__main__":
    main()
