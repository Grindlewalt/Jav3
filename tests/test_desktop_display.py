"""The live desktop, P1 (watch only): the image recipe, the guest listener's
session lifecycle (Xvnc mocked with scripts), the host's RFB input filter, the
status / start refusals, and the WebSocket splice against a fake guest socket.
Offline; no VM, no vsock, no X."""
import io
import json
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from backend import auth
from backend.config import settings
from backend.vm import boxes, display_api, images
from backend.vm.guest_pkg import build_package_tar

ROOT = Path(__file__).resolve().parent.parent


# --- the image recipe ---------------------------------------------------------------

def test_desktop_recipe_adds_the_display_stack_and_the_jav3_cli_libs():
    r = images.parse_recipe((ROOT / "vm/images/desktop.recipe").read_text())
    have = {(p["manager"], p["package"]): p["version"] for p in r["packages"]}
    for apt in ("tigervnc-standalone-server", "xterm", "openbox", "xdotool", "maim", "tmux",
                "xvfb", "chromium", "scrot", "python3-pil"):       # the old ones stay
        assert ("apt", apt) in have, apt
    assert r["min_mem_mb"] == 1280 and r["from"] == "main"
    # exact pins (the recipe takes no ranges) inside the jav3 client's own ranges
    from packaging.specifiers import SpecifierSet
    install = (ROOT / "clients/jav3cli/install.sh").read_text()
    for name, var in (("httpx", "HTTPX_SPEC"), ("textual", "TUI_SPECS")):
        spec = re.search(rf"^{var}='{name}([^']+)'", install, re.M).group(1)
        assert have[("pip", name)] in SpecifierSet(spec), (name, spec)


def test_box_json_names_the_display_listener(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    boxes.registry.reset()
    try:
        b = boxes.allocate("project", project="d", variant="desktop")
        assert b.box_json()["listen"]["display"] == {"transport": "vsock", "port": 5559}
        assert boxes.PORT_DISPLAY == 5559
        d = boxes.allocate("project", project="dd", runtime="docker") \
            if settings.docker_enabled else None
        assert d is None or d.box_json()["listen"]["display"]["path"] == "/run/jav3/5559.sock"
    finally:
        boxes.registry.reset()


def test_guest_package_ships_the_display_listener():
    with tarfile.open(fileobj=io.BytesIO(build_package_tar()), mode="r:gz") as t:
        names = t.getnames()
    assert "backend/display.py" in names and "backend/memguard.py" in names
    srv = (ROOT / "guest/backend/server.py").read_text()
    assert "display.serve()" in srv       # started like shell.py, best-effort


# --- the guest listener: session lifecycle with Xvnc mocked -----------------------------

FAKE_XVNC = """#!{py}
import os, socket, sys
a = sys.argv
sock = a[a.index("-rfbunixpath") + 1]
xdir = os.environ["FAKE_X11_DIR"]
os.makedirs(xdir, exist_ok=True)
open(os.path.join(xdir, "X" + a[1][1:]), "w").close()
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("xvnc %d %s\\n" % (os.getpid(), " ".join(a[1:])))
srv = socket.socket(socket.AF_UNIX)
srv.bind(sock)
srv.listen(5)
while True:
    c, _ = srv.accept()
    c.sendall(b"RFB 003.008\\n")
    c.sendall(b"echo:" + c.recv(100))
    while c.recv(4096):
        pass
    c.close()
"""

FAKE_APP = """#!{py}
import os, sys, time
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("%s %d %s DISPLAY=%s\\n" % (os.path.basename(sys.argv[0]), os.getpid(),
                                       " ".join(sys.argv[1:]), os.environ.get("DISPLAY")))
time.sleep(600)
"""

GUEST_SCRIPT = r'''
import asyncio, json, os, socket, sys, time
from backend import display

root = sys.argv[1]
display.RFB_SOCK = root + "/d.sock"
display.APPS_PATH = root + "/apps.json"
display.X11_DIR = root + "/x11"
display.IDLE_STOP_S = 0.6
display.START_WAIT_S = 5
display.MIN_FREE_MB = 0
log = os.environ["FAKE_LOG"]

def pids(prefix):
    out = []
    for line in open(log):
        w = line.split()
        if w and w[0] == prefix:
            out.append(int(w[1]))
    return out

def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

async def until(cond, secs=15):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()

async def ask(mode, extra=b""):
    loop = asyncio.get_running_loop()
    a, b = socket.socketpair()
    a.setblocking(False)
    t = asyncio.ensure_future(display._handle(loop, b))
    await loop.sock_sendall(a, json.dumps({"mode": mode}).encode() + b"\n" + extra)
    return loop, a, t

async def line(loop, a):
    buf = b""
    while b"\n" not in buf:
        buf += await loop.sock_recv(a, 4096)
    l, _, rest = buf.partition(b"\n")
    return l, rest

async def main():
    s = display.session
    loop = asyncio.get_running_loop()
    # off, nothing running
    loop, a, t = await ask("status")
    st = json.loads((await line(loop, a))[0])
    assert st["ok"] and st["installed"] and not st["running"] and st["geometry"] == "1280x800", st
    a.close(); await t

    # a viewer starts it: ack, then the RFB bytes of Xvnc, and our bytes reach it
    loop, a, t = await ask("rfb", b"hello")
    ack, rest = await line(loop, a)
    assert json.loads(ack) == {"ok": True}, ack
    buf = rest
    while b"echo:hello" not in buf:
        buf += await loop.sock_recv(a, 4096)
    assert buf.startswith(b"RFB 003.008\n"), buf
    assert s.running() and s.viewers == 1
    argv = open(log).read().split("xvnc ", 1)[1].splitlines()[0]
    for flag in ("-SecurityTypes None", "-rfbunixpath " + root + "/d.sock", "-rfbport -1",
                 "-geometry 1280x800", "-SendCutText=0", "-AcceptCutText=0", ":100"):
        assert flag in argv, (flag, argv)
    assert await until(lambda: len(pids("openbox")) == 1 and len(pids("xterm")) == 1), \
        "wm + first window"
    xterm = [l for l in open(log) if l.startswith("xterm")][0]
    assert "tmux new-session -A -s desk" in xterm and "DISPLAY=:100" in xterm, xterm
    doc = json.load(open(display.APPS_PATH))
    assert doc["display"] == ":100" and doc["geometry"] == [1280, 800]
    assert set(doc["apps"]) == {"terminal", "browser"} and doc["tmux_session"] == "desk"
    br = doc["apps"]["browser"]["argv"]
    for f in ("--disable-background-networking", "--disable-sync", "--no-first-run", "--no-sandbox"):
        assert f in br, f
    assert any(x.startswith("--proxy-server=http://") for x in br), br
    assert doc["apps"]["terminal"]["argv"][0] == "xterm"
    x_pid, wm_pid = pids("xvnc")[0], pids("openbox")[0]

    # the viewer stays: past the idle window the display is still up
    await asyncio.sleep(1.0)
    assert s.running() and alive(x_pid), "stopped under a viewer"
    assert s.status()["idle_stop_in_s"] is None

    # the viewer leaves: after IDLE_STOP_S the session stops, with everything on it
    a.close(); await t
    assert s.viewers == 0
    assert await until(lambda: not s.running() and not os.path.exists(display.RFB_SOCK))
    assert not os.path.exists(display.APPS_PATH)
    assert await until(lambda: not alive(x_pid) and not alive(wm_pid)
                       and not alive(pids("xterm")[0])), "left processes"

    # start again by hand; no viewer ever comes: it stops itself
    loop, a, t = await ask("start")
    st = json.loads((await line(loop, a))[0])
    assert st["running"] and st["viewers"] == 0 and st["idle_stop_in_s"] is not None, st
    a.close(); await t
    assert await until(lambda: not s.running()), "no viewer ever came, still up"

    # it dies by itself (killed): the rest is taken down, the next start is clean
    await s.ensure()
    first = s._x.pid
    os.kill(first, 9)
    for _ in range(50):
        if not s.running() and s._x is None:
            break
        await asyncio.sleep(0.1)
    assert not s.running() and s._procs == [], "half a display left"
    await s.ensure()
    assert s.running() and s._x.pid != first

    # explicit stop
    loop, a, t = await ask("stop")
    st = json.loads((await line(loop, a))[0])
    assert not st["running"], st
    a.close(); await t

    # what a refusal looks like: no tigervnc in the image
    os.environ["PATH"] = root + "/empty"
    loop, a, t = await ask("start")
    r = json.loads((await line(loop, a))[0])
    assert r["ok"] is False and "no desktop" in r["error"] and "rebuild" in r["error"], r
    a.close(); await t
    loop, a, t = await ask("status")
    assert json.loads((await line(loop, a))[0])["installed"] is False
    a.close(); await t
    # ... and a box too low on memory
    os.environ["PATH"] = root + "/bin:" + os.environ["PATH"]
    display.MIN_FREE_MB = 160
    display._mem_available_mb = lambda: 50
    loop, a, t = await ask("rfb")
    r = json.loads((await line(loop, a))[0])
    assert r["ok"] is False and "50 MB" in r["error"] and "160 MB" in r["error"], r
    a.close(); await t
    # an unknown mode answers, never hangs
    loop, a, t = await ask("nope")
    r = json.loads((await line(loop, a))[0])
    assert r["ok"] is False and "nope" in r["error"]
    a.close(); await t
    print("LIFECYCLE-OK")

asyncio.run(main())
'''


def test_guest_display_session_lifecycle(tmp_path):
    import shutil
    import tempfile
    short = Path(tempfile.mkdtemp(prefix="d1-", dir="/tmp"))     # sun_path is ~104 bytes
    try:
        pkg = tmp_path / "pkg"
        with tarfile.open(fileobj=io.BytesIO(build_package_tar()), mode="r:gz") as t:
            t.extractall(pkg, filter="data")
        bin_dir = short / "bin"
        bin_dir.mkdir()
        (short / "empty").mkdir()
        for name, body in (("Xtigervnc", FAKE_XVNC), ("openbox", FAKE_APP), ("xterm", FAKE_APP)):
            f = bin_dir / name
            f.write_text(body.format(py=sys.executable))
            f.chmod(f.stat().st_mode | stat.S_IXUSR)
        (short / "script.py").write_text(GUEST_SCRIPT)
        (short / "log").write_text("")
        env = {"PYTHONPATH": str(pkg), "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
               "FAKE_LOG": str(short / "log"), "FAKE_X11_DIR": str(short / "x11")}
        r = subprocess.run([sys.executable, "-S", str(short / "script.py"), str(short)],
                           cwd=pkg, env=env, capture_output=True, text=True, timeout=90)
        assert r.returncode == 0 and "LIFECYCLE-OK" in r.stdout, \
            r.stdout[-800:] + r.stderr[-1500:]
        for line in (short / "log").read_text().splitlines():     # nothing left running
            w = line.split()
            if w and w[0] in ("xvnc", "openbox", "xterm"):
                try:
                    os.kill(int(w[1]), 9)
                except OSError:
                    pass
    finally:
        shutil.rmtree(short, ignore_errors=True)


# --- the RFB client -> guest filter --------------------------------------------------

def key(down=1, sym=0x61):
    return struct.pack(">BBHI", 4, down, 0, sym)


def pointer(x=10, y=20, mask=1):
    return struct.pack(">BBHH", 5, mask, x, y)


def cut_text(text=b"secret"):
    return struct.pack(">BBBBI", 6, 0, 0, 0, len(text)) + text


def ext_cut_text(n):
    return struct.pack(">BBBBi", 6, 0, 0, 0, -n) + b"x" * n


FBU = struct.pack(">BBHHHH", 3, 1, 0, 0, 1280, 800)
ENCODINGS = struct.pack(">BBH3i", 2, 0, 3, 0, 1, -239)
PIXFMT = bytes([0, 0, 0, 0]) + bytes(16)
FENCE = struct.pack(">B3xIB", 248, 1 << 31, 3) + b"abc"
CONT = struct.pack(">BBHHHH", 150, 1, 0, 0, 1280, 800)
QEMU_KEY = struct.pack(">BBHII", 255, 0, 1, 0x61, 30)
EXT_POINTER = struct.pack(">BBHHB", 5, 0x81, 10, 20, 1)          # marker bit: 7 bytes
RESIZE = struct.pack(">BxHHBx", 251, 800, 600, 1) + bytes(16)
XVP = bytes([250, 0, 1, 2])
HANDSHAKE = b"RFB 003.008\n" + b"\x01" + b"\x01"


def _feed_all(flt, data, step=None):
    if step is None:
        return flt.feed(data)
    return b"".join(flt.feed(data[i:i + step]) for i in range(0, len(data), step))


def test_filter_watch_only_drops_every_kind_of_input_and_passes_the_rest():
    flt = display_api.RfbInputFilter()
    watch = FBU + ENCODINGS + PIXFMT + FENCE + CONT
    stream = (HANDSHAKE + key() + watch + pointer() + cut_text() + QEMU_KEY + EXT_POINTER
              + ext_cut_text(40) + RESIZE + XVP + FBU)
    out = flt.feed(stream)
    assert out == HANDSHAKE + watch + FBU
    assert flt.dropped == {"key": 2, "pointer": 2, "cut_text": 2, "resize": 1, "xvp": 1}


@pytest.mark.parametrize("step", [1, 2, 3, 7, 64])
def test_filter_gives_the_same_answer_however_the_bytes_arrive(step):
    watch = FBU + ENCODINGS + FENCE
    stream = (HANDSHAKE + watch + key() + pointer() + EXT_POINTER + cut_text(b"x" * 300)
              + QEMU_KEY + watch)
    flt = display_api.RfbInputFilter()
    assert _feed_all(flt, stream, step) == HANDSHAKE + watch + watch
    assert flt.dropped == {"key": 2, "pointer": 2, "cut_text": 1}


def test_filter_skips_a_huge_clipboard_without_holding_it():
    flt = display_api.RfbInputFilter()
    flt.feed(HANDSHAKE)
    mib = 1024 * 1024
    n = 5 * mib + 1000
    assert flt.feed(struct.pack(">BBBBI", 6, 0, 0, 0, n) + b"a" * 1000) == b""
    for _ in range(5):
        assert flt.feed(b"b" * mib) == b""
        assert len(flt._buf) == 0                  # skipped as it arrives, never held
    assert flt._skip == 0
    assert flt.feed(FBU) == FBU                    # and the stream is in step again
    assert flt.dropped["cut_text"] == 1


def test_filter_policy_hook_is_where_a_control_holder_plugs_in():
    seen = []
    holder = {"key": True, "pointer": True}

    def allow(kind):
        seen.append(kind)
        return holder.get(kind, False)
    flt = display_api.RfbInputFilter(allow)
    out = flt.feed(HANDSHAKE + key() + pointer() + cut_text() + EXT_POINTER + RESIZE + XVP)
    assert out == HANDSHAKE + key() + pointer() + EXT_POINTER      # input passes while held
    assert flt.dropped == {"cut_text": 1, "resize": 1, "xvp": 1}   # clipboard, resize, xvp never
    assert "resize" not in seen and "xvp" not in seen             # never even asked
    holder["key"] = False                                          # control handed back
    assert flt.feed(key()) == b""


def test_filter_fails_closed_on_what_no_viewer_sends():
    for bad in (b"GET / HTTP/1.1\r\n", b"RFB 003.009\n", b"RFB 004.000\n"):
        with pytest.raises(display_api.RfbViolation):
            display_api.RfbInputFilter().feed(bad)
    with pytest.raises(display_api.RfbViolation):               # VNC auth is not offered
        display_api.RfbInputFilter().feed(b"RFB 003.008\n\x02")
    flt = display_api.RfbInputFilter()
    flt.feed(HANDSHAKE)
    for bad in (bytes([9]), bytes([255, 7]), bytes([1])):          # unknown lengths
        with pytest.raises(display_api.RfbViolation):
            display_api.RfbInputFilter._frame(bytearray(bad + bytes(20)))
    with pytest.raises(display_api.RfbViolation):
        flt.feed(struct.pack(">BBBBi", 6, 0, 0, 0, -(1 << 30)))    # absurd clipboard
    # 3.3 has no security byte; 3.7 has one
    out = display_api.RfbInputFilter().feed(b"RFB 003.003\n\x01" + FBU)
    assert out == b"RFB 003.003\n\x01" + FBU
    out = display_api.RfbInputFilter().feed(b"RFB 003.007\n\x01\x01" + key() + FBU)
    assert out == b"RFB 003.007\n\x01\x01" + FBU


# --- the host API: status, refusals, the WebSocket splice ------------------------------

class FakeCtl:
    def __init__(self, running=True):
        self._running = running
        self.acquired = 0
        self.released = 0
        self.inflight = 0

    def running(self):
        return self._running

    async def acquire(self):
        self.acquired += 1

    def release(self):
        self.released += 1

    async def teardown(self):
        self._running = False


@pytest.fixture
def host(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_max_boxes", 6)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 3)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 4096)
    boxes.registry.reset()
    app = FastAPI()
    app.include_router(display_api.router)
    tc = TestClient(app)
    tc.cookies.set(auth.COOKIE_NAME, auth.make_token(7, "grant"))
    display_api._viewers.clear()
    yield tc
    boxes.registry.reset()
    display_api._viewers.clear()


def _desktop_box(running=True, mem=1280):
    b = boxes.allocate("project", project="game", variant="desktop", mem_mb=mem)
    b.ctl = FakeCtl(running)
    return b


class FakeGuest(threading.Thread):
    """The guest's display listener: answers the hello, then speaks RFB. It
    reads until the host hangs up, and sends one frame once `expect` bytes of
    the browser's stream have reached it."""
    def __init__(self, sock, ack=b'{"ok":true}\n', banner=b"RFB 003.008\n", expect=0):
        super().__init__(daemon=True)
        self.sock, self.ack, self.banner, self.expect = sock, ack, banner, expect
        self.hello = b""
        self.got = b""
        self.done = threading.Event()

    def run(self):
        try:
            while b"\n" not in self.hello:
                self.hello += self.sock.recv(4096)
            self.sock.sendall(self.ack + self.banner)
            if not json.loads(self.ack).get("ok"):
                return
            self.sock.settimeout(5)
            sent = False
            while True:
                d = self.sock.recv(65536)
                if not d:
                    break
                self.got += d
                if not sent and len(self.got) >= self.expect:
                    self.sock.sendall(b"FRAME-1")
                    sent = True
        except OSError:
            pass
        finally:
            self.done.set()


def _wire_guest(monkeypatch, **kw):
    host_end, guest_end = socket.socketpair()
    host_end.setblocking(False)
    guest = FakeGuest(guest_end, **kw)
    calls = []

    async def connect(self, port):
        calls.append(port)
        return host_end
    monkeypatch.setattr(boxes.VsockTransport, "connect", connect)
    return guest, calls


def test_ws_splices_the_guest_and_drops_every_input_message(host, monkeypatch):
    b = _desktop_box()
    expected = HANDSHAKE + FBU + FBU
    guest, calls = _wire_guest(monkeypatch, expect=len(expected))
    guest.start()
    from backend import bus
    q = bus.subscribe(boxes.BUS_CHAN)
    with host.websocket_connect(f"/api/vm/boxes/{b.id}/display/ws",
                                subprotocols=["binary"]) as ws:
        assert ws.accepted_subprotocol == "binary"
        assert ws.receive_bytes() == b"RFB 003.008\n"            # the guest's banner
        assert display_api._viewers == {b.id: 1}
        ws.send_bytes(HANDSHAKE)
        ws.send_bytes(key() + pointer() + cut_text())             # a modified client
        ws.send_bytes(FBU)
        ws.send_bytes(QEMU_KEY + EXT_POINTER + RESIZE + FBU)
        assert ws.receive_bytes() == b"FRAME-1"                   # guest -> browser, untouched
    assert guest.done.wait(5)
    assert guest.hello == b'{"mode":"rfb"}\n' and calls == [boxes.PORT_DISPLAY]
    assert guest.got == expected, guest.got                       # no 4 / 5 / 6 / 251 / 255 got in
    assert b.ctl.acquired == 1 and b.ctl.released == 1            # pinned while open, then freed
    assert display_api._viewers == {}
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    bus.unsubscribe(boxes.BUS_CHAN, q)
    assert {"type": "display", "box_id": b.id, "viewers": 1} in events
    assert {"type": "display", "box_id": b.id, "viewers": 0} in events


def test_ws_ends_a_session_that_stops_speaking_rfb(host, monkeypatch):
    b = _desktop_box()
    guest, _ = _wire_guest(monkeypatch, expect=10 ** 9)
    guest.start()
    with host.websocket_connect(f"/api/vm/boxes/{b.id}/display/ws") as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"
        ws.send_bytes(HANDSHAKE)
        ws.send_bytes(b"GET / HTTP/1.1\r\n")                     # not a viewer
        msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == display_api.CLOSE_PROTOCOL
        assert "protocol error" in msg["reason"]
    assert guest.done.wait(5)
    assert guest.got == HANDSHAKE                                 # only what was legal, up to there
    assert b.ctl.released == 1


def test_ws_refuses_without_a_cookie_a_stopped_box_a_docker_box_and_a_plain_image(host, monkeypatch):
    b = _desktop_box(running=False)
    guest, calls = _wire_guest(monkeypatch)
    # no session cookie: refused before it is accepted
    from starlette.websockets import WebSocketDisconnect
    anon = TestClient(host.app)
    with pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect(f"/api/vm/boxes/{b.id}/display/ws"):
            pass
    # a stopped box is never booted by a viewer
    with host.websocket_connect(f"/api/vm/boxes/{b.id}/display/ws") as ws:
        msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == display_api.CLOSE_REFUSED
        assert "stopped" in msg["reason"]
    assert b.ctl.acquired == 0 and calls == []                # never pinned, never dialed
    # a box on another image
    p = boxes.allocate("project", project="plain", variant="main")
    p.ctl = FakeCtl(True)
    with host.websocket_connect(f"/api/vm/boxes/{p.id}/display/ws") as ws:
        msg = ws.receive()
        assert msg["code"] == display_api.CLOSE_REFUSED and "`main` image" in msg["reason"]
    # an unknown box
    with host.websocket_connect("/api/vm/boxes/p-nope/display/ws") as ws:
        assert ws.receive()["code"] == display_api.CLOSE_REFUSED


def test_ws_tells_the_page_when_the_guest_cannot_start_it(host, monkeypatch):
    b = _desktop_box()
    guest, _ = _wire_guest(monkeypatch, ack=b'{"ok":false,"error":"no tigervnc: rebuild"}\n',
                           banner=b"")
    guest.start()
    with host.websocket_connect(f"/api/vm/boxes/{b.id}/display/ws") as ws:
        msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == display_api.CLOSE_GUEST
        assert "no tigervnc" in msg["reason"]
    assert b.ctl.acquired == 1 and b.ctl.released == 1


def test_status_says_what_starting_would_cost(host):
    b = _desktop_box(running=False)
    r = host.get(f"/api/vm/boxes/{b.id}/display")
    assert r.status_code == 200
    j = r.json()
    cost = 1280 + settings.vm_kvm_box_overhead_mb
    assert j["supported"] and j["state"] == "stopped" and j["session"] == "stopped"
    assert j["need_mb"] == cost and j["free_mb"] == 4096 and j["fits"] is True
    assert j["geometry"] == {"width": 1280, "height": 800} and j["watch_only"] is True
    assert host.get("/api/vm/boxes/p-none/display").status_code == 404
    anon = TestClient(host.app)
    assert anon.get(f"/api/vm/boxes/{b.id}/display").status_code in (401, 403)


def test_start_refuses_when_the_ram_budget_cannot_fit_the_box(host, monkeypatch):
    b = _desktop_box(running=False)
    from backend.vm import lifecycle
    shared = FakeCtl(True)
    monkeypatch.setattr(lifecycle, "vm", shared)               # the shared box is running
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 2000)
    started = []

    async def fake_start(box):
        started.append(box.id)
    monkeypatch.setattr(boxes, "start", fake_start)
    cost = 1280 + settings.vm_kvm_box_overhead_mb
    free = 2000 - (768 + settings.vm_kvm_box_overhead_mb)
    r = host.post(f"/api/vm/boxes/{b.id}/display")
    assert r.status_code == 409
    assert r.json()["detail"].startswith(f"the desktop box needs {cost} MB, {free} MB free")
    assert started == []                                       # nothing was booted
    j = host.get(f"/api/vm/boxes/{b.id}/display").json()
    assert j["fits"] is False and j["need_mb"] == cost and j["free_mb"] == free


def test_start_boots_a_stopped_box_then_starts_the_display(host, monkeypatch):
    b = _desktop_box(running=False)
    order = []

    async def fake_start(box):
        order.append("boot")
        box.ctl._running = True
    monkeypatch.setattr(boxes, "start", fake_start)

    async def fake_call(box, mode, timeout=10.0):
        order.append(mode)
        return {"ok": True, "installed": True, "running": True, "viewers": 0}
    monkeypatch.setattr(display_api, "_guest_call", fake_call)
    r = host.post(f"/api/vm/boxes/{b.id}/display")
    assert r.status_code == 200, r.text
    assert order[:2] == ["boot", "start"]
    assert r.json()["session"] == "running" and r.json()["state"] == "running"
    assert r.json()["need_mb"] == 0                            # running: nothing more to boot


def test_start_refuses_a_docker_box_and_a_plain_image(host, monkeypatch):
    b = boxes.allocate("project", project="game", variant="desktop", mem_mb=1280)
    b.runtime = "docker"                                       # a record is enough for the gate
    b.ctl = FakeCtl(False)
    r = host.post(f"/api/vm/boxes/{b.id}/display")
    assert r.status_code == 409
    assert "Docker box has no desktop" in r.json()["detail"] and "KVM" in r.json()["detail"]
    j = host.get(f"/api/vm/boxes/{b.id}/display").json()
    assert j["supported"] is False and "KVM" in j["reason"]
    p = boxes.allocate("project", project="plain", variant="main")
    p.ctl = FakeCtl(False)
    r = host.post(f"/api/vm/boxes/{p.id}/display")
    assert r.status_code == 409 and "`main` image" in r.json()["detail"]
    assert "desktop" in r.json()["detail"]


def test_start_names_a_guest_that_does_not_answer_and_one_without_tigervnc(host, monkeypatch):
    b = _desktop_box(running=True)

    async def refuse(box, mode, timeout=10.0):
        raise ConnectionError("connection refused")
    monkeypatch.setattr(display_api, "_guest_call", refuse)
    r = host.post(f"/api/vm/boxes/{b.id}/display")
    assert r.status_code == 502 and "restart it" in r.json()["detail"]
    j = host.get(f"/api/vm/boxes/{b.id}/display").json()
    assert j["session"] == "unavailable" and "Restart" in j["note"]

    async def nodesktop(box, mode, timeout=10.0):
        return {"ok": False, "error": "this box's image has no desktop: rebuild the `desktop` image"}
    monkeypatch.setattr(display_api, "_guest_call", nodesktop)
    r = host.post(f"/api/vm/boxes/{b.id}/display")
    assert r.status_code == 409 and "rebuild" in r.json()["detail"]
