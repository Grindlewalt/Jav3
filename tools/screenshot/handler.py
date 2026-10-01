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

async def _exec(argv: list[str], *, timeout: float, env: dict | None = None,
                cwd: str | None = None) -> int:
    memguard.setup()        # chromium is what outgrew a desktop box (2026-10-01)
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, cwd=cwd, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        preexec_fn=memguard.confine)
    try:
        return await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        _killpg(proc.pid)
        await proc.wait()
        return -9


def _killpg(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


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
        rc = await _exec(chromium_argv(binary, url, raw, width, h, wait_ms, proxy, prof,
                                       root=os.geteuid() == 0),
                         timeout=wait_ms / 1000 + 45)
    if rc != 0 and not os.path.exists(raw):
        return f"error: chromium failed (exit {rc}) loading {url}."
    note = " [remote content: this turn is now tainted]" if remote else ""
    return _finish(raw, f"{url}{note}")


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
    await xvfb.ensure(width, height)
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
