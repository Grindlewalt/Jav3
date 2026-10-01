"""Guest live desktop: a VNC X display the operator can watch in the web app.

A fourth host-dialed listener (PORT 5559, box.json `listen.display`) next to
run-turn (5556), shell (5557) and svcd (5558). The host broker splices a
browser's noVNC session to it; nothing here faces the network. The first line
the host sends is one JSON object naming a mode, and the reply is one JSON
line:

    {"mode":"rfb"}     start the display if it is off, reply {"ok":true}, then
                       splice the connection to Xvnc's unix socket (raw RFB
                       bytes both ways, until either side hangs up)
    {"mode":"start"}   start it (idempotent), reply the status
    {"mode":"status"}  reply the status, change nothing
    {"mode":"stop"}    stop the display and what runs on it, reply the status
    {"mode":"desk"}    the agent's seat (P2): needs the display UP (it never
                       starts it), replies {"ok":true}, then speaks the desk
                       wire protocol as JSON lines (backend/desk.py) with
                       jav3-desk's Session running as a child (deskbox.py)

Errors reply {"ok":false,"error":"<what and what to do>"} and close. A desk
connection holds the display (`Session.hold()`) while requests arrive and for
HOLD_GRACE_S after the last one, so the idle timer cannot stop the screen under
the agent; it ends when the display stops.

The display is one Xvnc (:100, 1280x800 on purpose: the agent's screenshots are
that size), an openbox window manager and, as a first thing to look at, an xterm
attached to the tmux session `desk`. It is started lazily and stopped after
IDLE_STOP_S with no viewer, so a box nobody watches pays nothing for it. Every
process runs under memguard.confine (the work cgroup + the highest
oom_score_adj): when memory runs out the desktop is what dies, not the run-turn
server.

Agents can already put apps on it: anything run with DISPLAY=:100 appears. The
two the desk tool will launch by name are in `apps()` and, for P2, written to
APPS_PATH as JSON when the display starts.
"""
import asyncio
import json
import os
import shutil
import signal
import socket
import sys
import time

from . import boxinfo, memguard

PORT = 5559
DISPLAY = ":100"
WIDTH, HEIGHT = 1280, 800
RFB_SOCK = "/run/jav3-display.sock"       # Xvnc's -rfbunixpath; never TCP
X11_DIR = "/tmp/.X11-unix"                # where Xvnc makes the X socket
APPS_PATH = "/run/jav3-desk-apps.json"
IDLE_STOP_S = 300                         # no viewer for this long: stop the display
MIN_FREE_MB = 160                         # Xvnc + openbox + a terminal, with room
START_WAIT_S = 15
XVNC_NAMES = ("Xtigervnc", "Xvnc")        # Debian's tigervnc ships the first
AUTOSTART = ("terminal",)                 # apps() names opened with the display
TMUX_SESSION = "desk"
PY_VENV_BIN = "/opt/jav3/py/bin"          # the image's pip venv (httpx, textual)
HOLD_GRACE_S = 60                         # a desk request keeps the screen up this long
PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # holds `backend/`
# the agent's seat: jav3-desk's Session as a child on DISPLAY=:100 (deskbox.py).
# A list so the tests can stand a fake one in.
DESK_ARGV = [sys.executable, "-m", "backend.deskbox"]


class DisplayError(Exception):
    """The display cannot start; the message says what to do about it."""


def _mem_available_mb() -> int | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _find_xvnc() -> str | None:
    for name in XVNC_NAMES:
        p = shutil.which(name)
        if p:
            return p
    return None


def xvnc_argv(exe: str) -> list[str]:
    """Xvnc listening on a unix socket only, no security (the socket is
    reachable only from inside the box, and the host splices it), no clipboard
    either way, and a fixed size the viewer cannot change."""
    return [exe, DISPLAY, "-geometry", f"{WIDTH}x{HEIGHT}", "-depth", "24",
            "-SecurityTypes", "None", "-rfbunixpath", RFB_SOCK,
            "-rfbunixmode", "0600", "-rfbport", "-1", "-nolisten", "tcp",
            "-AlwaysShared", "-SendCutText=0", "-AcceptCutText=0",
            "-SendPrimary=0", "-SetPrimary=0", "-AcceptSetDesktopSize=0"]


def apps() -> dict:
    """The apps the agent (P2) and the desktop launch by name: {name: {argv,
    env}}. Both run with DISPLAY set; the browser goes through the box's egress
    proxy like everything else the box runs."""
    proxy = boxinfo.net()["proxy"]
    return {
        # the same session whoever types: the operator in the window, the agent
        # with `tmux send-keys -t desk`
        "terminal": {"argv": [
            "xterm", "-tn", "xterm-256color", "-fa", "DejaVu Sans Mono", "-fs", "11",
            "-geometry", "130x38+0+0", "-e", "tmux", "new-session", "-A",
            "-s", TMUX_SESSION], "env": {}},
        "browser": {"argv": [
            "chromium", "--disable-background-networking", "--disable-sync",
            "--no-first-run", f"--proxy-server={proxy}", "--no-sandbox",
            # a throwaway profile (the box is the sandbox), a /dev/shm that
            # does not fill up, no keyring prompt, and the whole screen
            "--user-data-dir=/tmp/jav3-desk-chromium", "--disable-dev-shm-usage",
            "--password-store=basic", f"--window-size={WIDTH},{HEIGHT}",
            "--window-position=0,0"], "env": {}},
    }


def _app_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    if os.path.isdir(PY_VENV_BIN):
        path = f"{PY_VENV_BIN}:{path}"
    env.update(DISPLAY=DISPLAY, HOME=env.get("HOME") or "/root", PATH=path,
               LANG="C.UTF-8")
    env.update(extra or {})
    return env


def _killpg(pid: int, sig=signal.SIGKILL) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class Session:
    """The one display of this box: its processes, its viewers, its idle timer."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._x = None                    # the Xvnc process
        self._procs: list = []            # openbox and the autostarted apps
        self.viewers = 0
        self.desks = 0                    # agent desk connections (mode "desk")
        self._holds = 0
        self._timer = None
        self._idle_at: float | None = None
        self.started_at: float | None = None

    # -- state ----------------------------------------------------------------
    def running(self) -> bool:
        return self._x is not None and self._x.returncode is None

    def status(self) -> dict:
        left = None
        if self._idle_at is not None:
            left = max(0, int(self._idle_at - time.monotonic()))
        return {"ok": True, "installed": _find_xvnc() is not None,
                "running": self.running(), "viewers": self.viewers, "desks": self.desks,
                "display": DISPLAY, "geometry": f"{WIDTH}x{HEIGHT}",
                "idle_stop_in_s": left,
                "uptime_s": (int(time.monotonic() - self.started_at)
                             if self.running() and self.started_at else None)}

    # -- start / stop -----------------------------------------------------------
    async def ensure(self) -> None:
        """Start the display if it is not up. Raises DisplayError."""
        async with self._lock:
            if not self.running():
                await self._start_locked()
        self._arm()

    async def _spawn(self, argv: list[str], env: dict | None = None, *,
                     cwd: str | None = None, piped: bool = False):
        # found on this process's PATH, not the app env's (which puts the image's
        # pip venv first and is only for what runs inside the display)
        argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]
        pipe = asyncio.subprocess.PIPE if piped else asyncio.subprocess.DEVNULL
        return await asyncio.create_subprocess_exec(
            *argv, env=env if env is not None else _app_env(), cwd=cwd,
            stdin=pipe, stdout=pipe,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
            preexec_fn=memguard.confine)

    async def _start_locked(self) -> None:
        exe = _find_xvnc()
        if exe is None:
            raise DisplayError(
                "this box's image has no desktop (tigervnc is not installed): rebuild "
                "the `desktop` image, then restart the box")
        free = _mem_available_mb()
        if free is not None and free < MIN_FREE_MB:
            raise DisplayError(
                f"the box has {free} MB of memory free and the desktop needs about "
                f"{MIN_FREE_MB} MB: stop what is running in it, or give the project "
                "a bigger box")
        await self._stop_locked()                # leftovers of a crashed one
        sock_x = f"{X11_DIR}/X{DISPLAY[1:]}"
        for p in (RFB_SOCK, f"/tmp/.X{DISPLAY[1:]}-lock", sock_x):
            _unlink(p)
        memguard.setup()
        self._x = await self._spawn(xvnc_argv(exe))
        for _ in range(START_WAIT_S * 10):
            if self._x.returncode is not None:
                break
            if os.path.exists(RFB_SOCK) and os.path.exists(sock_x):
                break
            await asyncio.sleep(0.1)
        if not self.running() or not os.path.exists(RFB_SOCK):
            await self._stop_locked()
            raise DisplayError("the display did not start (Xvnc exited): the box may "
                               "be out of memory")
        self.started_at = time.monotonic()
        asyncio.ensure_future(self._watch(self._x))
        # a window manager (without one, windows have no titles and cannot be
        # moved), then the first thing to look at. Best-effort: a missing one
        # leaves a working, plainer display.
        try:
            self._procs.append(await self._spawn(["openbox"]))
        except OSError:
            pass
        await self._write_apps()
        for name in AUTOSTART:
            spec = apps().get(name)
            if spec:
                try:
                    self._procs.append(await self._spawn(
                        spec["argv"], _app_env(spec.get("env"))))
                except OSError:
                    pass

    async def _write_apps(self) -> None:
        """desk-apps.json: what P2's desk tool may launch by name."""
        doc = {"v": 1, "display": DISPLAY, "geometry": [WIDTH, HEIGHT],
               "tmux_session": TMUX_SESSION, "autostart": list(AUTOSTART),
               "apps": apps()}
        try:
            with open(APPS_PATH, "w") as f:
                json.dump(doc, f)
        except OSError:
            pass

    async def _watch(self, proc) -> None:
        """Xvnc died by itself (out of memory, killed): take the rest down so the
        next viewer starts a clean display instead of a half one."""
        await proc.wait()
        if self._x is proc:
            async with self._lock:
                if self._x is proc:
                    await self._stop_locked()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._idle_at = None
        procs, self._procs = self._procs, []
        x, self._x = self._x, None
        everything = ([x] if x is not None else []) + procs
        for p in everything:
            if p.returncode is None:
                _killpg(p.pid, signal.SIGTERM)
        for p in everything:
            try:
                await asyncio.wait_for(p.wait(), 2)
            except asyncio.TimeoutError:
                _killpg(p.pid)
                try:
                    await asyncio.wait_for(p.wait(), 2)
                except asyncio.TimeoutError:
                    pass
        self.started_at = None
        for p in (RFB_SOCK, APPS_PATH):
            _unlink(p)

    # -- who is using it --------------------------------------------------------
    def viewer_up(self) -> None:
        self.viewers += 1
        self._arm()

    def viewer_down(self) -> None:
        self.viewers = max(0, self.viewers - 1)
        self._arm()

    def hold(self) -> None:
        """The agent is working on the display, keep it up (pair with
        release(), like a viewer)."""
        self._holds += 1
        self._arm()

    def release(self) -> None:
        self._holds = max(0, self._holds - 1)
        self._arm()

    def _arm(self) -> None:
        """Start the idle clock when nobody uses a running display, stop it
        when somebody does."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
            self._idle_at = None
        if not self.running() or self.viewers or self._holds:
            return
        loop = asyncio.get_running_loop()
        self._idle_at = time.monotonic() + IDLE_STOP_S
        self._timer = loop.call_later(IDLE_STOP_S, lambda: loop.create_task(self._idle()))

    async def _idle(self) -> None:
        if not self.viewers and not self._holds:
            await self.stop()


session = Session()


# --- the listener ------------------------------------------------------------------

async def _read_line(loop, conn) -> tuple[bytes, bytes]:
    buf = b""
    while b"\n" not in buf:
        if len(buf) > 65536:
            return b"", b""
        chunk = await loop.sock_recv(conn, 65536)
        if not chunk:
            break
        buf += chunk
    line, _, rest = buf.partition(b"\n")
    return line, rest


async def _reply(loop, conn, doc: dict) -> None:
    await loop.sock_sendall(conn, (json.dumps(doc) + "\n").encode())


async def _pump(loop, src, dst) -> None:
    while True:
        data = await loop.sock_recv(src, 65536)
        if not data:
            return
        await loop.sock_sendall(dst, data)


async def _splice(loop, conn, first: bytes) -> None:
    """Raw bytes between the host's connection and Xvnc's unix socket."""
    x = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    x.setblocking(False)
    session.viewer_up()
    try:
        await loop.sock_connect(x, RFB_SOCK)
        if first:
            await loop.sock_sendall(x, first)
        tasks = {asyncio.ensure_future(_pump(loop, conn, x)),
                 asyncio.ensure_future(_pump(loop, x, conn))}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
    finally:
        session.viewer_down()
        x.close()


class _DeskHold:
    """The display stays up while the agent's requests arrive and HOLD_GRACE_S
    after the last: one `Session.hold()` taken on the first request, released
    by a timer that every request pushes back (and at once when the connection
    ends)."""

    def __init__(self):
        self._held = False
        self._timer = None

    def touch(self) -> None:
        if not self._held:
            self._held = True
            session.hold()
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.get_running_loop().call_later(HOLD_GRACE_S, self.drop)

    def drop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._held:
            self._held = False
            session.release()


async def _to_child(loop, conn, proc, rest: bytes, hold: _DeskHold) -> None:
    """Host -> the desk child, line by line. A request is the agent working:
    it holds the display. Anything that is not a JSON object is dropped."""
    buf = rest
    while True:
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "req":
                hold.touch()
            proc.stdin.write(line + b"\n")
            await proc.stdin.drain()
        if len(buf) > 1 << 20:
            return
        data = await loop.sock_recv(conn, 65536)
        if not data:
            return
        buf += data


async def _from_child(loop, conn, proc) -> None:
    while True:
        data = await proc.stdout.read(65536)
        if not data:
            return
        await loop.sock_sendall(conn, data)


async def _until_stopped() -> None:
    while session.running():
        await asyncio.sleep(1)


async def _desk(loop, conn, rest: bytes) -> None:
    """The agent's seat. Refused unless the operator started the screen (the
    RAM decision is theirs); ends when it stops."""
    if not session.running():
        await _reply(loop, conn, {"ok": False, "error":
                     "the desktop is not running: start it (Work > Desktop) first"})
        return
    env = {**_app_env(), "PYTHONPATH": PKG_ROOT, "JAV3_DESK_APPS": APPS_PATH}
    try:
        proc = await session._spawn(DESK_ARGV, env, cwd=PKG_ROOT, piped=True)
    except OSError as e:
        await _reply(loop, conn, {"ok": False, "error": f"the desk client did not start: {e}"})
        return
    hold = _DeskHold()
    session.desks += 1
    tasks = [asyncio.ensure_future(c) for c in (
        _to_child(loop, conn, proc, rest, hold), _from_child(loop, conn, proc),
        _until_stopped())]
    try:
        await _reply(loop, conn, {"ok": True})
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
            if t.done() and not t.cancelled():
                t.exception()               # a pipe that broke: nothing to report
        _killpg(proc.pid)
        session.desks -= 1
        hold.drop()
        try:
            await asyncio.wait_for(proc.wait(), 2)
        except asyncio.TimeoutError:
            pass


async def _handle(loop, conn) -> None:
    conn.setblocking(False)
    try:
        line, rest = await _read_line(loop, conn)
        try:
            req = json.loads(line or b"{}")
        except json.JSONDecodeError:
            req = {}
        mode = req.get("mode") if isinstance(req, dict) else None
        try:
            if mode == "rfb":
                await session.ensure()
                await _reply(loop, conn, {"ok": True})
                await _splice(loop, conn, rest)
            elif mode == "start":
                await session.ensure()
                await _reply(loop, conn, session.status())
            elif mode == "status":
                await _reply(loop, conn, session.status())
            elif mode == "stop":
                await session.stop()
                await _reply(loop, conn, session.status())
            elif mode == "desk":
                await _desk(loop, conn, rest)
            else:
                await _reply(loop, conn, {"ok": False,
                                          "error": f"unknown display mode {mode!r}"})
        except DisplayError as e:
            await _reply(loop, conn, {"ok": False, "error": str(e)})
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


async def serve() -> None:
    loop = asyncio.get_running_loop()
    if boxinfo.unix_gateway():
        print("GUEST-DISPLAY-SERVER: not started (a docker box has no desktop)", flush=True)
        return
    s = boxinfo.listen("display", PORT)
    print(f"GUEST-DISPLAY-SERVER: listening on vsock :{PORT}", flush=True)
    while True:
        conn, _ = await loop.sock_accept(s)
        asyncio.ensure_future(_handle(loop, conn))
