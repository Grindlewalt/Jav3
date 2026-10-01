"""screenshot: capture a page or a GUI app INSIDE the box (DESIGN-BOXES 2(g)).

Runs only in the guest (pushed with the in-guest tools). Two modes:

  url  headless chromium (`--headless=new --screenshot`), no display needed,
       all traffic through the box's egress proxy (loopback stays local).
  app  an on-demand `Xvfb :99` (no window manager, -nolisten tcp), the app run
       with DISPLAY=:99 from argv (never a shell), captured with scrot or
       Pillow; the app is killed after the capture, Xvfb after 5 idle minutes.

The image is downscaled to <= 1280 px wide, PNG, or JPEG q80 when the PNG is
over 1 MB, and returned with imageresult.with_image (the in-guest loop
re-attaches it as a user image block).

Taint: a non-loopback URL, or an app that opened any connection to the egress
proxy while it ran (new entries in /proc/net/tcp, TIME_WAIT included), is
remote content: the gateway `taint_note` op marks the turn tainted exactly as
web_read does. If the note cannot be delivered the image is withheld.

On an image without chromium / Xvfb the tool says it needs the `desktop`
variant and does nothing else.
"""
import asyncio
import io
import ipaddress
import json
import os
import shutil
import signal
import socket
import time
from typing import NamedTuple
from urllib.parse import urlsplit

from backend import memguard

MAX_W = 1280
MAX_H = 4096
MAX_BYTES = 1_000_000
WAIT_MAX = 15_000
W_MAX, H_MAX = 1600, 1200
FULL_PAGE_H = 4000
DISPLAY = ":99"
XVFB_IDLE_S = 300
SHOT_DIR = "/tmp/jav3-shots"
DEFAULT_PROXY = "http://10.201.0.1:8443"
URL_EXTRA_S = 45         # chromium's allowance on top of wait_ms: start-up, load, capture
OOM_POLL_S = 1.0         # how often a running chromium is checked for a kernel memory kill
OOM_GRACE_S = 3.0        # after the first kill chromium may still finish; then it is stopped
CHROMIUM_FLOOR_MB = 400  # measured 2026-10-01 (chromium 154, desktop box): a blank page, all processes
NEEDS_DESKTOP = ("error: screenshot needs the `desktop` image variant (chromium, Xvfb, "
                 "scrot), which this box does not run. Ask the operator to set this "
                 "project's security profile image to `desktop`.")


# --- pure helpers (tested on the host) -----------------------------------------

def clamp(wait_ms, width, height) -> tuple[int, int, int]:
    def i(v, d):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d
    return (max(0, min(i(wait_ms, 2000), WAIT_MAX)),
            max(200, min(i(width, 1280), W_MAX)),
            max(200, min(i(height, 800), H_MAX)))


def check_url(url) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url mode needs `url`")
    u = url.strip()
    if any(ord(c) < 33 for c in u):
        raise ValueError("url must not contain spaces or control characters")
    parts = urlsplit(u)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url must be http:// or https:// with a host")
    return u


def is_loopback_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def argv_mentions_remote(argv: list[str]) -> bool:
    for a in argv:
        for tok in str(a).replace("=", " ").split():
            if "://" in tok:
                try:
                    if not is_loopback_url(check_url(tok)):
                        return True
                except ValueError:
                    return True
    return False


def encode_image(data: bytes, *, max_w: int = MAX_W, max_h: int = MAX_H,
                 max_bytes: int = MAX_BYTES) -> tuple[bytes, str, tuple[int, int]]:
    """Downscale to <= max_w wide (and <= max_h tall), PNG, or JPEG q80 when
    the PNG is over max_bytes. Returns (bytes, mime, (w, h))."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    w, h = im.size
    scale = min(1.0, max_w / w, max_h / h)
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    if buf.tell() <= max_bytes:
        return buf.getvalue(), "image/png", im.size
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=80, optimize=True)
    return buf.getvalue(), "image/jpeg", im.size


def _hex_addr(h: str) -> tuple[str, int]:
    ip, port = h.split(":")
    raw = bytes.fromhex(ip)
    if len(raw) == 4:
        addr = socket.inet_ntoa(raw[::-1])
    else:   # tcp6: four little-endian 32-bit words
        words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
        a6 = ipaddress.IPv6Address(words)
        addr = str(a6.ipv4_mapped or a6)
    return addr, int(port, 16)


def proxy_conns(proxy_host: str, proxy_port: int,
                paths=("/proc/net/tcp", "/proc/net/tcp6")) -> set[tuple[str, int]]:
    """Every socket (any state, TIME_WAIT included) whose remote end is the
    proxy: {(local_addr, local_port)}."""
    out = set()
    for p in paths:
        try:
            with open(p) as f:
                lines = f.read().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            parts = ln.split()
            if len(parts) < 3:
                continue
            try:
                loc, rem = _hex_addr(parts[1]), _hex_addr(parts[2])
            except ValueError:
                continue
            if rem == (proxy_host, proxy_port):
                out.add(loc)
    return out


def proxy_addr() -> tuple[str, str, int]:
    """(proxy URL, host, port) from box.json, else the env, else the shared
    box's proxy."""
    url = ""
    try:
        with open("/opt/jarvis/box.json") as f:
            url = (json.load(f).get("net") or {}).get("proxy") or ""
    except (OSError, ValueError):
        pass
    url = url or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or DEFAULT_PROXY
    p = urlsplit(url)
    return url, p.hostname or "10.201.0.1", p.port or 8443


def chromium_argv(binary: str, url: str, out: str, width: int, height: int,
                  wait_ms: int, proxy: str, profile_dir: str, *, root: bool) -> list[str]:
    argv = [binary, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--disable-dev-shm-usage", "--disable-background-networking",
            "--disable-sync", "--metrics-recording-only", "--mute-audio",
            f"--user-data-dir={profile_dir}", f"--window-size={width},{height}",
            f"--virtual-time-budget={max(wait_ms, 1)}", f"--screenshot={out}",
            f"--proxy-server={proxy}"]
    if root:
        argv.append("--no-sandbox")     # the box is the sandbox; chromium refuses root otherwise
    argv.append(url)                    # checked: starts with http(s)://, never a flag
    return argv


# --- guest runtime --------------------------------------------------------------

class Ran(NamedTuple):
    """How a child ended. `rc` is -9 when this tool killed it (timeout, or the
    kernel's memory kill showed in the work cgroup and it did not finish in time)."""
    rc: int
    timed_out: bool = False
    oom_kills: int = 0      # processes the kernel OOM-killed in the work cgroup meanwhile
    secs: float = 0.0


async def _exec(argv: list[str], *, timeout: float, env: dict | None = None,
                cwd: str | None = None, watch_oom: bool = False) -> Ran:
    """Run argv in the work cgroup. With `watch_oom` the cgroup's kill counter is
    read every second: after the kernel kills one of chromium's processes for
    memory the rest usually hang until the timeout (2026-10-01, seven 60 s waits
    in one turn), so it is given OOM_GRACE_S to finish and is stopped after that."""
    memguard.setup()        # chromium is what outgrew a desktop box (2026-10-01)
    kills0 = memguard.oom_kills()
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, cwd=cwd, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        preexec_fn=memguard.confine)
    timed_out = False
    first_kill = None
    while True:
        left = timeout - (time.monotonic() - t0)
        if left <= 0:
            timed_out = True
            break
        try:
            await asyncio.wait_for(proc.wait(), min(OOM_POLL_S, left) if watch_oom else left)
            break
        except asyncio.TimeoutError:
            if not watch_oom:
                timed_out = True
                break
        memguard.confine_session(proc.pid)      # chromium lowers its children's adj itself
        now = memguard.oom_kills()
        if kills0 is not None and now is not None and now > kills0:
            first_kill = first_kill or time.monotonic()
            if time.monotonic() - first_kill >= OOM_GRACE_S:
                break
    killed = proc.returncode is None
    if killed:
        _killpg(proc.pid)
        await proc.wait()
    now = memguard.oom_kills()
    kills = now - kills0 if kills0 is not None and now is not None and now > kills0 else 0
    return Ran(-9 if killed else proc.returncode, timed_out, kills, time.monotonic() - t0)


def _killpg(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


# --- why a capture failed (the model gets the cause and what to change) ----------

def failure_kind(ran: Ran) -> str:
    if ran.oom_kills:
        return "oom"            # also when the timeout came first: the kill is the cause
    if ran.timed_out:
        return "timeout"
    if ran.rc == -9:
        return "killed"
    return "exit"


def failure_text(kind: str, url: str, ran: Ran, limit_mb: int = 0, repeat: int = 1,
                 same_call: bool = False) -> str:
    """The tool result for a capture that made no image. `repeat` is how many
    times in a row this turn this page failed this way; `same_call` is True when
    the arguments were identical to the previous failure."""
    secs = f"{ran.secs:.0f} s"
    smaller = ("Use a smaller width and height, make the page lighter (lower view distance, "
               "fewer or smaller textures and buffers, no shadow maps), or ask the operator "
               "to raise this project's RAM (its placement, Runs in memory).")
    if kind == "oom":
        cap = f"the {limit_mb} MB this box lets a command use" if limit_mb else "this box's memory"
        room = (f" Chromium itself takes about {CHROMIUM_FLOOR_MB} MB for any page, so this "
                f"page had about {limit_mb - CHROMIUM_FLOOR_MB} MB."
                if limit_mb >= CHROMIUM_FLOOR_MB + 100 else "")
        body = (f"chromium was killed for using more than {cap}, {secs} into loading {url}."
                f"{room} The turn and the box are fine. {smaller}")
    elif kind == "timeout":
        body = (f"chromium timed out after {secs} loading {url} and was killed (it gets "
                f"wait_ms + {URL_EXTRA_S} s). The kernel killed nothing for memory: the page was "
                "still working. While it waits chromium draws the page's frames faster than "
                "real time, so a longer wait_ms means more frames to draw, not more time for "
                "each one. Try a shorter wait_ms (2000 to 4000), a smaller width and height, "
                "or lower the page's own render settings.")
    elif kind == "killed":
        body = (f"chromium was killed (SIGKILL) {secs} into loading {url}, not by this tool's "
                "timeout. In this box that is almost always the kernel running out of memory. "
                f"{smaller}")
    else:
        body = (f"chromium failed (exit {ran.rc}) loading {url}. It made no image: the page may "
                "have crashed it, or nothing is listening at that address (is the server still "
                "running in this box?).")
    head = ""
    if repeat >= 2:
        head = (f"same failure as the previous call ({repeat} in a row this turn), don't retry "
                "unchanged. " + ("" if same_call else
                                 "Changing only the page's parameters has not helped. "))
    return "error: " + head + body


_fails: dict = {"op": None, "pages": {}}    # this turn's failures: page -> [kind, args, count]


def _turn_id():
    try:
        from backend import turnctx
        return turnctx.op_id.get()
    except Exception:  # noqa: BLE001 -- no turn context (host tests): one turn
        return None


def _page_key(url: str) -> str:
    return url.split("#", 1)[0].split("?", 1)[0]


def note_failure(kind: str, url: str, args: tuple) -> tuple[int, bool]:
    """Record a failed capture: (how many in a row of this kind for this page in
    this turn, whether the arguments equal the previous failure's)."""
    op = _turn_id()
    if _fails["op"] != op:
        _fails["op"], _fails["pages"] = op, {}
    rec = _fails["pages"].get(_page_key(url))
    if rec and rec[0] == kind:
        same = rec[1] == args
        rec[1], rec[2] = args, rec[2] + 1
        return rec[2], same
    _fails["pages"][_page_key(url)] = [kind, args, 1]
    return 1, False


def note_success(url: str) -> None:
    _fails["pages"].pop(_page_key(url), None)


async def taint_note(source: str) -> bool:
    """Gateway `taint_note` for this turn. True only on {"type":"taint_noted"}."""
    from backend import turnctx
    req = {"op": "taint_note", "op_id": turnctx.op_id.get(),
           "op_token": turnctx.op_token.get(), "source": source[:64]}
    port = turnctx.gateway_port.get()

    def call() -> dict:
        s = None
        try:
            from backend import boxinfo       # WP1: knows vsock vs unix
            s = boxinfo.gateway_connect()
        except ImportError:
            s = None
        if s is None:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            s.settimeout(15)
            s.connect((socket.VMADDR_CID_HOST, port))
        try:
            s.settimeout(15)
            s.sendall((json.dumps(req) + "\n").encode())
            return json.loads(s.makefile("rb").readline() or b"{}")
        finally:
            s.close()
    try:
        rep = await asyncio.get_running_loop().run_in_executor(None, call)
    except (OSError, ValueError):
        return False
    return rep.get("type") == "taint_noted"


class _Xvfb:
    def __init__(self):
        self.proc = None
        self.geom = None
        self.timer = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def ensure(self, w: int, h: int) -> None:
        if self.running() and self.geom == (w, h):
            return
        await self.stop()
        memguard.setup()
        self.proc = await asyncio.create_subprocess_exec(
            "Xvfb", DISPLAY, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
            preexec_fn=memguard.confine)
        self.geom = (w, h)
        sock = f"/tmp/.X11-unix/X{DISPLAY[1:]}"
        for _ in range(50):
            if os.path.exists(sock) or not self.running():
                break
            await asyncio.sleep(0.1)
        if not self.running():
            raise RuntimeError("Xvfb did not start")

    def arm_idle(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
        loop = asyncio.get_running_loop()
        self.timer = loop.call_later(XVFB_IDLE_S, lambda: loop.create_task(self.stop()))

    async def stop(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.running():
            _killpg(self.proc.pid)
            await self.proc.wait()
        self.proc, self.geom = None, None


xvfb = _Xvfb()


def _out_path(ext: str = "png") -> str:
    os.makedirs(SHOT_DIR, exist_ok=True)
    return os.path.join(SHOT_DIR, f"shot-{int(time.time() * 1000)}.{ext}")


def _finish(raw_path: str, caption: str) -> str:
    from backend.agent import imageresult
    try:
        with open(raw_path, "rb") as f:
            raw = f.read()
    except OSError:
        return "error: the capture produced no image."
    if not raw:
        return "error: the capture produced an empty image."
    try:
        data, mime, (w, h) = encode_image(raw)
    except ImportError:
        return "error: Pillow is missing from this image (the desktop variant has it)."
    except Exception as e:  # noqa: BLE001 — a corrupt capture must not kill the turn
        return f"error: could not read the capture ({type(e).__name__})."
    out = _out_path("png" if mime == "image/png" else "jpg")
    with open(out, "wb") as f:
        f.write(data)
    try:
        os.unlink(raw_path)
    except OSError:
        pass
    text = f"screenshot {w}x{h} {mime.split('/')[1]} ({len(data) // 1024} KB): {caption}"
    return imageresult.with_image(text, out, caption=caption[:200])


async def _url_mode(url: str, wait_ms: int, width: int, height: int,
                    full_page: bool) -> str:
    binary = shutil.which("chromium") or shutil.which("chromium-browser")
    if not binary:
        return NEEDS_DESKTOP
    try:
        url = check_url(url)
    except ValueError as e:
        return f"error: {e}"
    remote = not is_loopback_url(url)
    if remote and not await taint_note("screenshot:url"):
        return ("error: could not record that this turn read remote content, so the "
                "screenshot was not taken.")
    proxy, _, _ = proxy_addr()
    raw = _out_path("raw.png")
    h = FULL_PAGE_H if full_page else height
    import tempfile
    with tempfile.TemporaryDirectory(prefix="jav3-chromium-") as prof:
        ran = await _exec(chromium_argv(binary, url, raw, width, h, wait_ms, proxy, prof,
                                        root=os.geteuid() == 0),
                          timeout=wait_ms / 1000 + URL_EXTRA_S, watch_oom=True)
    if not isinstance(ran, Ran):
        ran = Ran(int(ran))
    if ran.rc != 0 and not os.path.exists(raw):
        kind = failure_kind(ran)
        repeat, same = note_failure(kind, url, (url, wait_ms, width, height, bool(full_page)))
        return failure_text(kind, url, ran, memguard.limit_now_mb(), repeat, same)
    note = " [remote content: this turn is now tainted]" if remote else ""
    if ran.oom_kills:
        note += (" [the kernel killed a process for memory while this ran: the image may be "
                 "incomplete]")
    out = _finish(raw, f"{url}{note}")
    if not out.startswith("error"):
        note_success(url)
    return out


async def _app_mode(command, wait_ms: int, width: int, height: int) -> str:
    if not shutil.which("Xvfb"):
        return NEEDS_DESKTOP
    if (not isinstance(command, list) or not command or len(command) > 64
            or not all(isinstance(a, str) and a and "\x00" not in a for a in command)):
        return "error: app mode needs `command` as a non-empty list of strings (argv)."
    if not shutil.which(command[0]) and not os.path.exists(command[0]):
        return f"error: {command[0]!r} is not installed in this box."
    from backend.agent.tools import toolctx
    from backend.config import settings
    slug = await toolctx.active_slug()
    cwd = settings.projects_dir / (slug or "_scratch")
    cwd.mkdir(parents=True, exist_ok=True)
    _, phost, pport = proxy_addr()
    before = proxy_conns(phost, pport)
    await xvfb.ensure(width, height)        # also makes the work cgroup (memguard.setup)
    kills0 = memguard.oom_kills()
    env = {**os.environ, "DISPLAY": DISPLAY}
    proc = await asyncio.create_subprocess_exec(
        *command, env=env, cwd=str(cwd), stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True, preexec_fn=memguard.confine)
    raw = _out_path("raw.png")
    try:
        await asyncio.sleep(wait_ms / 1000)
        if shutil.which("scrot"):
            await _exec(["scrot", "-o", "-z", raw], timeout=20, env=env)
        if not os.path.exists(raw):
            try:
                from PIL import ImageGrab
                ImageGrab.grab(xdisplay=DISPLAY).save(raw, "PNG")
            except Exception:  # noqa: BLE001
                pass
    finally:
        _killpg(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            pass
        xvfb.arm_idle()
    kills1 = memguard.oom_kills()
    killed = kills1 is not None and kills0 is not None and kills1 > kills0
    if killed and not os.path.exists(raw):
        cap = memguard.limit_now_mb()
        return ("error: the app or its display was killed for using more than "
                + (f"the {cap} MB this box lets a command use" if cap else "this box's memory")
                + ", so there is no image. The turn and the box are fine. Use a smaller "
                  "width and height, a lighter app, or ask the operator to raise this "
                  "project's RAM (its placement, Runs in memory).")
    remote = argv_mentions_remote(command) or bool(proxy_conns(phost, pport) - before)
    if remote and not await taint_note("screenshot:app"):
        try:
            os.unlink(raw)
        except OSError:
            pass
        return ("error: the app contacted the network and the taint could not be "
                "recorded, so the screenshot is withheld.")
    note = " [the app fetched remote content: this turn is now tainted]" if remote else ""
    return _finish(raw, f"{' '.join(command)[:120]}{note}")


async def run(mode: str = "url", url: str | None = None, command=None,
              wait_ms: int = 2000, width: int = 1280, height: int = 800,
              full_page: bool = False) -> str:
    try:
        from backend.config import settings
        in_guest = bool(getattr(settings, "in_guest", False))
    except ImportError:
        in_guest = False
    if not in_guest:
        return ("error: screenshot only runs inside the sandbox box. The guest loop is "
                "not active on this turn.")
    wait_ms, width, height = clamp(wait_ms, width, height)
    if mode == "url":
        return await _url_mode(url, wait_ms, width, height, bool(full_page))
    if mode == "app":
        return await _app_mode(command, wait_ms, width, height)
    return "error: mode must be `url` or `app`."
