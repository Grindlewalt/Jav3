"""Admin CLI:
  python -m backend.cli setup [--add-user] [--username U --password-stdin]
                             [--provider ID [--api-key-stdin] [--base-url URL]] [--no-test]
                                                     # first-run setup (login + model provider)
  python -m backend.cli create-user <username> [password]  # omit it: hidden prompt
  python -m backend.cli guest-shell [project-slug]   # drop into the sandbox guest
  python -m backend.cli services-check               # probe the companion services
  python -m backend.cli paths [name]                 # resolved state paths
  python -m backend.cli migrate-state [--to DIR]     # move checkout state to the state dir
  python -m backend.cli backup [--if-configured]     # rclone the state to the remote
  python -m backend.cli restore [REMOTE] [--to DIR] [--secrets|--no-secrets] [--force]
  python -m backend.cli import-skill <folder|https-git-url[#subdir]|clawhub:slug> [--name N] [--replace]
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
    elif len(sys.argv) >= 2 and sys.argv[1] == "setup":
        setup_command(sys.argv[2:])
    elif len(sys.argv) >= 2 and sys.argv[1] == "guest-shell":
        guest_shell(sys.argv[2] if len(sys.argv) > 2 else None)
    elif len(sys.argv) >= 2 and sys.argv[1] == "services-check":
        services_check()
    elif len(sys.argv) >= 2 and sys.argv[1] == "paths":
        paths(sys.argv[2] if len(sys.argv) > 2 else None)
    elif len(sys.argv) >= 2 and sys.argv[1] == "import-skill":
        import_skill_command(sys.argv[2:])
    elif len(sys.argv) >= 2 and sys.argv[1] in ("migrate-state", "backup", "restore"):
        state_command(sys.argv[1], sys.argv[2:])
    else:
        print(__doc__.split("\n", 1)[1].rstrip())
        sys.exit(1)


def server_urls() -> list[str]:
    """Where to open the GUI: the mDNS name the running server advertises
    (`<instance>.local`), then each LAN IP, all on the configured port."""
    from . import lan
    from .config import settings
    try:
        from .main import app
        title = app.title
    except Exception:          # noqa: BLE001 — a URL hint is never fatal
        title = ""
    port = settings.lan_port
    hosts = [f"{lan.instance_name(title)}.local"] if settings.mdns else []
    try:
        hosts += lan.lan_ips()
    except Exception:          # noqa: BLE001
        pass
    return [f"http://{h}:{port}/" for h in dict.fromkeys(hosts)] \
        or [f"http://localhost:{port}/"]


def _ask(prompt: str, default: str = "") -> str:
    try:
        v = input(prompt).strip()
    except EOFError:
        sys.exit("\nsetup: input ended")
    return v or default


def _yes(prompt: str, default: bool) -> bool:
    v = _ask(f"{prompt} [{'Y/n' if default else 'y/N'}] ").lower()
    return default if not v else v.startswith("y")


def _stdin_line(what: str) -> str:
    line = sys.stdin.readline()
    if not line:
        sys.exit(f"setup: expected the {what} on stdin")
    return line.rstrip("\r\n")


def setup_command(args: list[str]) -> None:
    """The TUI twin of the web /setup page: login, model provider, finish.
    Interactive on a terminal; scriptable with the flags (secrets on stdin,
    one per line, password first — never in argv, where `ps` can read them)."""
    import argparse
    from . import setup_api
    from .setup_api import SetupError

    ap = argparse.ArgumentParser(prog="python -m backend.cli setup")
    ap.add_argument("--username")
    ap.add_argument("--password-stdin", action="store_true")
    ap.add_argument("--provider", help="provider id, or 'none' to skip")
    ap.add_argument("--api-key-stdin", action="store_true")
    ap.add_argument("--base-url")
    ap.add_argument("--no-test", action="store_true", help="skip the test call")
    ap.add_argument("--add-user", action="store_true",
                    help="add another login even though setup is done")
    a = ap.parse_args(args)
    tty = sys.stdin.isatty()
    scripted = a.password_stdin or a.api_key_stdin

    asyncio.run(init_db())
    exists = asyncio.run(setup_api.users_exist())
    if exists and not a.add_user:
        print("setup is already done (a login exists). To add another login: "
              "python -m backend.cli setup --add-user", file=sys.stderr)
        sys.exit(1)
    if not tty and not (a.username and a.password_stdin):
        print("setup: no terminal — pass --username and --password-stdin",
              file=sys.stderr)
        sys.exit(2)

    # 1. the login
    print("\n1. Create your login")
    username = a.username or _ask("   username: ")
    if a.password_stdin:
        password = _stdin_line("password")
    else:
        print(f"   (at least {setup_api.MIN_PASSWORD} characters; a few "
              "unrelated words beat one clever one)")
        password = _prompt_password()
    try:
        username = setup_api.validate_credentials(username, password)
    except SetupError as e:
        sys.exit(f"setup: {e.detail}")

    # 2. the provider (skipped by --add-user unless one is named)
    pid = key = base = ""
    if a.provider:
        pid = "" if a.provider.lower() == "none" else a.provider.strip().lower()
    elif tty and not scripted and not a.add_user:
        cat = setup_api.catalogue()
        print("\n2. Connect a model provider")
        for i, p in enumerate(cat, 1):
            print(f"   {i}) {p['label']}")
        print("   0) skip for now (add one later in Settings)")
        while True:
            pick = _ask("   choose a number: ", "0")
            if pick.isdigit() and 0 <= int(pick) <= len(cat):
                break
            print("   not on the list")
        pid = cat[int(pick) - 1]["id"] if int(pick) else ""
    if pid:
        try:
            entry = setup_api._entry(pid)
        except SetupError as e:
            sys.exit(f"setup: {e.detail}")
        if a.api_key_stdin:
            key = _stdin_line("API key").strip()
        elif entry["needs_key"] and tty:
            key = getpass.getpass("   API key (paste; hidden): ").strip()
        base = a.base_url or ""
        if not base and entry["needs_base_url"] and tty and not scripted:
            # a {VAR} in the catalogue URL: there is no usable default
            base = _ask(f"   base URL (like {entry['base_url']}): ")
        elif not base and not entry["needs_key"] and tty and not scripted:
            base = _ask(f"   base URL [{entry['base_url']}]: ")
        try:
            setup_api.check_provider(pid, key, base)
        except SetupError as e:
            sys.exit(f"setup: {e.detail}")
        print("   the key is stored on this server, never in the sandbox VM")
        run_test = (not a.no_test) and (not tty or scripted
                                       or _yes("   test it now?", True))
        if run_test:
            res = asyncio.run(setup_api.test_provider(pid, key, base))
            if res["ok"]:
                print(f"   ok · {len(res['models_found'])} models")
            else:
                print(f"   test failed: {res['detail']}")
                if not tty or scripted or not _yes("   save it anyway?", False):
                    sys.exit("setup: provider test failed (nothing was created; "
                             "--no-test skips the check)")

    # 3. finish
    try:
        if exists:
            asyncio.run(add_user(username, password))
        else:
            asyncio.run(setup_api.create_first_user(username, password))
    except SetupError as e:
        sys.exit(f"setup: {e.detail}")
    print(f"\nlogin '{username}' created")
    if pid:
        try:
            r = setup_api.store_provider(pid, key, base)
            print(f"provider {pid} saved" + (f" · default model {r['default']}"
                                             if r.get("default") else ""))
        except SetupError as e:
            print(f"provider not saved: {e.detail} (add it in Settings)")
    urls = server_urls()
    print("\nopen " + urls[0] + ("   (or " + ", ".join(urls[1:]) + ")"
                                  if len(urls) > 1 else ""))


async def add_user(username: str, password: str) -> None:
    """A further login; refuses an existing name (create-user overwrites)."""
    from .setup_api import SetupError
    await init_db()
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT OR IGNORE INTO users (username, password_hash) VALUES (?, ?)",
            (username, hash_password(password)))
        await db.commit()
        if cur.rowcount != 1:
            raise SetupError(409, f"a login named '{username}' already exists")
    finally:
        await db.close()


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


def import_skill_command(args: list[str]) -> None:
    """Vendor an OpenClaw skill into skills/oc-<name>/, pinned and NOT
    granted — grant it on the Tools page after reading it."""
    import argparse
    from .skillimport import SkillImportError, import_skill

    ap = argparse.ArgumentParser(prog="python -m backend.cli import-skill")
    ap.add_argument("source")
    ap.add_argument("--name", help="register under this tool name instead")
    ap.add_argument("--replace", action="store_true",
                    help="re-import over an existing copy (re-pins, revokes the grant)")
    a = ap.parse_args(args)
    asyncio.run(init_db())
    try:
        r = import_skill(a.source, name=a.name, replace=a.replace)
    except SkillImportError as e:
        print(f"import-skill: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"imported {r['name']} -> skills/{r['slug']} ({r['files']} files, "
          f"ref {r['ref'] or '-'}) — not granted")
    for f in r["flags"]:
        print(f"  flag  {f['trigger']:<14} {f['file']}")
    for q in r["requirements"]:
        print(f"  {'ok  ' if q['met'] else 'NEED'}  {q['kind']:<11} {q['name']}"
              + (f"  ({q['reason']})" if q["reason"] else ""))
    for h in r["install_hints"]:
        print(f"  hint  {h}  (not run)")
    if r["blocked"]:
        print(f"  blocked: {r['blocked']}")


if __name__ == "__main__":
    main()
