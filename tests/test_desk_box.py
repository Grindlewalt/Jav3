"""The agent drives a desktop box's screen with the desk tool (live desktop P2).

Host side (backend/vm/boxdesk.py + backend/desk.py): a box desk is registered
through a shim socket, gets its own device row and default grants, is seen only
by turns running in that box, and goes through the same act() as any computer
(fresh-frame rule, audit, taint). The "guest" on the other end of the socket is
the REAL jav3-desk Session (through guest/backend/deskbox.py) on a fake X11
backend. Guest side (guest/backend/display.py `desk` mode + the deskbox child):
hold/release of the display, ending with the screen, and the child against fake
xdotool / maim binaries. Offline: no VM, no vsock, no X."""
import asyncio
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import zlib
from pathlib import Path

import pytest

from backend import desk, devicetokens, runtime
from backend.agent import budget as budget_mod
from backend.agent.tools import registry
from backend.config import settings
from backend.db import get_db, init_db
from backend.vm import boxdesk, boxes, broker
from backend.vm.guest_pkg import build_package_tar

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


jd = _load("jav3desk_box", ROOT / "clients" / "jav3-desk" / "jav3-desk")
deskbox = _load("deskbox_under_test", ROOT / "guest" / "backend" / "deskbox.py")


def png(level: int, w: int = 1280, h: int = 800) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = (b"\x00" + bytes([level]) * w) * h
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeX(jd.Backend):
    """What xdotool + maim would do on :100: records the input, and the screen
    changes after every input (so `changed:` is yes)."""
    name, session = "x11", "x11"

    def __init__(self):
        super().__init__()
        self.calls: list = []
        self.n = 0

    def monitors(self):
        return [jd.Monitor("0", 0, 0, 1280, 800, 1)]

    def screenshot(self, mon, rect=None):
        return png(10 + self.n % 200)

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def click(self, button, count):
        self.calls.append(("click", button, count))
        self.n += 1

    def type_text(self, text):
        self.calls.append(("type", text))
        self.n += 1

    def key(self, combo):
        self.calls.append(("key", combo))
        self.n += 1

    def screen_state(self):
        return {"locked": False, "asleep": False}


class Guests:
    """The guest's display listener, one fake per connection: reads the
    `{"mode":"desk"}` line, acks, then runs deskbox over the socket."""

    def __init__(self, backend, apps=None, ack=None, announce=None):
        self.backend = backend
        self.apps = apps if apps is not None else {"terminal": ["xterm"]}
        self.ack, self.announce = ack, announce
        self.hellos = 0
        self.writers: list = []
        self.tasks: list = []

    async def connect(self, port):
        host_end, guest_end = socket.socketpair()
        host_end.setblocking(False)
        guest_end.setblocking(False)
        self.tasks.append(asyncio.ensure_future(self._seat(guest_end)))
        return host_end

    async def _seat(self, sock):
        reader, writer = await asyncio.open_connection(sock=sock)
        self.writers.append(writer)
        first = json.loads(await reader.readline())
        assert first == {"mode": "desk"}
        self.hellos += 1
        if self.ack is not None:
            writer.write(self.ack)
            await writer.drain()
            return
        writer.write(b'{"ok": true}\n')
        if self.announce is not None:
            writer.write(self.announce)
        await writer.drain()
        await deskbox.run(jd, self.backend, reader, writer, self.apps)

    def hang_up(self):
        for w in self.writers:
            w.close()


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(desk, "ROUND_GAP_S", 0)
    monkeypatch.setattr(jd, "SETTLE_S", 0.01)
    monkeypatch.setattr(jd, "SETTLE_MAX_S", 0.1)
    monkeypatch.setattr(jd, "SETTLE_POLL_S", 0.01)
    desk.reset_for_tests()
    boxdesk.reset_for_tests()
    yield
    desk.reset_for_tests()
    boxdesk.reset_for_tests()


@pytest.fixture
async def world(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_max_boxes", 6)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 3)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 4096)
    boxes.registry.reset()
    await init_db()
    fx = FakeX()
    guests = Guests(fx)

    async def connect(self, port):
        return await guests.connect(port)
    monkeypatch.setattr(boxes.VsockTransport, "connect", connect)
    yield {"fx": fx, "guests": guests,
           "box": boxes.allocate("project", project="game", variant="desktop", mem_mb=1280)}
    guests.hang_up()
    boxes.registry.reset()


class MacWS:
    """The operator's Mac, as far as desk.py can tell: a socket that takes text."""

    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self, code=1000):
        pass


async def _mac(device_id=9001):
    return await desk.attach(device_id, "macbook", MacWS(),
                             {"type": "hello", "v": 1, "backend": "mac", "platform": "darwin",
                              "apps": ["Safari"], "ceiling": {"screen": True, "input": True,
                                                              "shell": False}})


@contextlib.contextmanager
def turn(op, box=None, project=None):
    """A guest turn: the host's own binding of the op to its box, and the op as
    the running one (what the tool broker sets for a tool call)."""
    if box is not None:
        boxes.bind_op(op, box, project)
    tok = budget_mod.active_op_id.set(op)
    try:
        yield
    finally:
        budget_mod.active_op_id.reset(tok)
        if box is not None:
            boxes.unbind_op(op)
        broker._tainted.discard(op)


def _tool(name):
    return registry._load_dynamic(name)


async def _rows(sql, *args):
    db = await get_db()
    try:
        async with db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


# --- registration --------------------------------------------------------------------

async def test_box_desk_registers_with_its_own_row_and_default_grants(world):
    box = world["box"]
    d = await boxdesk.ensure(box)
    assert d.box_id == box.id and d.name == f"box:{box.id}" and d.shown == "sandbox"
    assert d.hello["backend"] == "x11" and d.hello["apps"] == ["terminal"]
    assert d.ceiling == {"screen": True, "input": True, "shell": False}
    (row,) = await _rows("SELECT * FROM device_tokens WHERE name = ?", f"box:{box.id}")
    assert row["scope"] == "desk" and row["paired_by"] == "box" and row["revoked"] == 0
    assert row["user_id"] is None and row["expires_at"].startswith("9999")
    assert len(row["token_hash"]) == 64          # a hash of a secret nobody kept
    assert d.device_id == row["id"]
    # the defaults: screen and input on, shell off
    assert await desk.get_grants(d.device_id) == {
        "screen": True, "input": True, "shell": "off", "trusted_until": None, "allowlist": []}
    assert d.grants["screen"] and d.grants["input"] and d.grants["shell"] == "off"
    # idempotent while it is up: the same desk, the same single row
    assert await boxdesk.ensure(box) is d and world["guests"].hellos == 1
    # it is in Settings -> Access -> Computer use
    (item,) = [o for o in await desk.overview() if o["id"] == d.device_id]
    assert item["online"] and item["name"] == f"box:{box.id}" and item["backend"] == "x11"
    assert boxdesk.state(box.id) == {"connected": True, "device_id": d.device_id, "error": None}
    # nothing can connect to /api/desk/ws with it
    assert await devicetokens.verify("jvd_" + "a" * 43) is None


async def test_the_operators_grants_survive_a_new_registration(world):
    box = world["box"]
    d = await boxdesk.ensure(box)
    await desk.set_grants(d.device_id, input=False)
    world["guests"].hang_up()
    for _ in range(100):
        if boxdesk.live(box.id) is None:
            break
        await asyncio.sleep(0.02)
    assert not desk.connected()
    d2 = await boxdesk.ensure(box)
    assert d2.device_id == d.device_id and d2 is not d
    assert d2.grants["input"] is False and d2.grants["screen"] is True
    assert len(await _rows("SELECT id FROM device_tokens WHERE name = ?", d.name)) == 1


async def test_ensure_says_why_the_seat_was_refused(world):
    box = world["box"]
    world["guests"].ack = b'{"ok": false, "error": "the desktop is not running: start it"}\n'
    with pytest.raises(boxdesk.BoxDeskError, match="not running"):
        await boxdesk.ensure(box)
    assert "not running" in boxdesk.state(box.id)["error"]
    assert not desk.connected()
    # a desk client that cannot start (no xdotool) says so in an error frame
    world["guests"].ack = None
    world["guests"].announce = b'{"type": "error", "error": "the x11 backend needs: xdotool"}\n'
    with pytest.raises(boxdesk.BoxDeskError, match="needs: xdotool"):
        await boxdesk.ensure(box)


async def test_a_status_read_that_finds_the_screen_up_registers_the_seat(world, monkeypatch):
    box = world["box"]
    boxdesk.sync(box)
    for _ in range(100):
        if boxdesk.live(box.id) is not None:
            break
        await asyncio.sleep(0.02)
    assert boxdesk.live(box.id) is not None
    boxdesk.sync(box)                                   # already there: no second dial
    await asyncio.sleep(0.05)
    assert world["guests"].hellos == 1


# --- who sees it ---------------------------------------------------------------------

async def test_only_turns_in_that_box_see_it_and_they_see_no_other_computer(world):
    box = world["box"]
    other = boxes.allocate("project", project="other", variant="main", mem_mb=1024)
    d = await boxdesk.ensure(box)
    mac = await _mac()
    # a turn in the desktop box: the sandbox, and not the Mac
    with turn("op-a", box, "game"):
        assert desk.offered()
        assert desk.resolve(None) is d
        assert desk.resolve("sandbox") is d and desk.resolve(f"box:{box.id}") is d
        with pytest.raises(desk.DeskError, match="connected: sandbox"):
            desk.resolve("macbook")
    # a turn in another project's box: the Mac, never this desktop
    with turn("op-b", other, "other"):
        assert desk.offered()
        assert desk.resolve(None) is mac
        with pytest.raises(desk.DeskError):
            desk.resolve("sandbox")
    # a host turn with no box bound: the same
    with turn("op-h"):
        assert desk.resolve(None) is mac
    # with the Mac gone, the other project and the host are offered nothing
    await desk.detach(mac)
    with turn("op-b", other, "other"):
        assert not desk.offered()
        with pytest.raises(desk.DeskError, match="no computer is connected"):
            desk.resolve(None)
    with turn("op-h"):
        assert not desk.offered()
    assert not desk.offered()                            # outside any turn (the Tools page)
    with turn("op-a", box, "game"):
        assert desk.offered()
        assert not desk.shell_offered()                  # shell is off for a box desktop


async def test_the_tools_are_listed_before_the_turn_is_bound_by_its_project(world):
    box = world["box"]
    await boxdesk.ensure(box)
    tok = runtime.active_project.set("game")
    try:
        assert desk.offered() and registry._requirements_met({"requires_desk": True})
    finally:
        runtime.active_project.reset(tok)
    tok = runtime.active_project.set("elsewhere")
    try:
        assert not desk.offered()
    finally:
        runtime.active_project.reset(tok)


async def test_a_box_whose_screen_stopped_never_falls_back_to_the_mac(world):
    box = world["box"]
    await boxdesk.ensure(box)
    mac = await _mac()
    world["guests"].hang_up()                            # the display stopped
    for _ in range(100):
        if boxdesk.live(box.id) is None:
            break
        await asyncio.sleep(0.02)
    with turn("op-a", box, "game"):
        assert not desk.offered()
        with pytest.raises(desk.DeskError, match="desktop is not running"):
            desk.resolve(None)
        assert "desktop is not running" in await desk.act("screenshot", {}, None)
    with turn("op-h"):
        assert desk.resolve(None) is mac


# --- the action path -----------------------------------------------------------------

async def test_act_round_trip_screenshot_click_type_key_through_the_seat(world):
    box, fx = world["box"], world["fx"]
    d = await boxdesk.ensure(box)
    with turn("op-a", box, "game"):
        # no blind input: a click before any screenshot is refused
        out = await _tool("desk_click")(x=5, y=5, computer="sandbox")
        assert out.startswith("error:") and "desk_screenshot" in out and not fx.calls
        shot = await _tool("desk_screenshot")(computer="sandbox")
        assert not shot.startswith("error") and "sandbox" in shot and "1280x800" in shot
        assert d.frame is not None and d.frame["w"] == 1280 and d.frame["serial"] == 1
        clicked = await _tool("desk_click")(x=100, y=200, computer="sandbox")
        assert not clicked.startswith("error"), clicked
        assert ("move", 100, 200) in fx.calls and ("click", "left", 1) in fx.calls
        assert "changed: yes" in clicked
        typed = await _tool("desk_type")(text="jav3 --server 127.0.0.1:8099", computer="sandbox")
        assert not typed.startswith("error"), typed
        assert ("type", "jav3 --server 127.0.0.1:8099") in fx.calls
        keyed = await _tool("desk_key")(combo="Return", computer="sandbox")
        assert not keyed.startswith("error"), keyed
        assert ("key", "Return") in fx.calls
        # shell is off by default, and the refusal names the sandbox
        sh = await _tool("desk_shell")(cmd="id", computer="sandbox")
        assert sh.startswith("error:") and "shell is off for 'sandbox'" in sh
        # taint: a screen is untrusted text, exactly as on the operator's computer
        assert broker.op_tainted("op-a")
    rows = await _rows("SELECT verb, ok, params, device_id FROM desk_actions ORDER BY id")
    assert [r["verb"] for r in rows if r["ok"]] == ["screenshot", "click", "type", "key"]
    assert all(r["device_id"] == d.device_id for r in rows)
    assert any(not r["ok"] for r in rows)                # the blind click and the shell
    typed_row = next(r for r in rows if r["verb"] == "type")
    assert "jav3" not in typed_row["params"]             # length + sha256, never the text
    assert json.loads(typed_row["params"])["len"] == len("jav3 --server 127.0.0.1:8099")


async def test_a_grant_turned_off_stops_the_agent_at_once(world):
    box, fx = world["box"], world["fx"]
    d = await boxdesk.ensure(box)
    with turn("op-a", box, "game"):
        await _tool("desk_screenshot")(computer="sandbox")
        await desk.set_grants(d.device_id, input=False)
        out = await _tool("desk_click")(x=1, y=1, computer="sandbox")
        assert "input is off for 'sandbox'" in out and not fx.calls
        # Stop (Settings): every grant off, the seat is told to drop input
        r = await desk.stop(d.device_id, by="operator")
        assert r["ok"]
    for _ in range(100):
        if boxdesk.live(box.id) is None:
            break
        await asyncio.sleep(0.02)
    assert not desk.connected()
    d2 = await boxdesk.ensure(box)                       # a new seat, still everything off
    assert d2.grants == {"screen": False, "input": False, "shell": "off",
                         "trusted_until": None, "allowlist": []}


async def test_the_seat_ends_when_the_guest_hangs_up(world):
    box = world["box"]
    d = await boxdesk.ensure(box)
    world["guests"].hang_up()
    for _ in range(100):
        if not desk.connected():
            break
        await asyncio.sleep(0.02)
    assert not desk.connected() and boxdesk.live(box.id) is None
    assert "box:" in (await _rows("SELECT name FROM device_tokens"))[0]["name"]
    with turn("op-a", box, "game"):
        assert "desktop is not running" in await _tool("desk_screenshot")(computer="sandbox")
    assert d.device_id                                   # the row outlives the connection


# --- the client hooks ----------------------------------------------------------------

def test_x11_thumbnail_through_pillow_when_imagemagick_is_missing(tmp_path, monkeypatch):
    from PIL import ImageGrab

    def no_x(**kw):
        raise OSError("no X support in this build")
    monkeypatch.setattr(ImageGrab, "grab", no_x)          # never the real screen
    maim = tmp_path / "maim"
    maim.write_text(f"#!{sys.executable}\nimport sys\n"
                    f"sys.stdout.buffer.write({png(90, 320, 200)!r})\n")
    maim.chmod(maim.stat().st_mode | stat.S_IXUSR)
    b = jd.X11Backend.__new__(jd.X11Backend)
    b.bin = {"maim": str(maim)}                           # no `import`
    thumb = b.thumbnail(jd.Monitor("0", 0, 0, 320, 200, 1))
    w, h, px = jd.decode_thumb(thumb)
    assert w == jd.THUMB_EDGE and h == 100 and set(px) == {90}
    # and a laptop without Pillow keeps the old byte-for-byte compare
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert b.pil_thumbnail(jd.Monitor("0", 0, 0, 320, 200, 1)) is None


# --- the guest: display.py `desk` mode + the deskbox child -------------------------------

FAKE_XDOTOOL = """#!{py}
import os, sys
a = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(" ".join(a) + "\\n")
if a[0] == "getdisplaygeometry":
    print("1280 800")
elif a[0] == "getmouselocation":
    print("X=1\\nY=2\\nSCREEN=0\\nWINDOW=0")
"""

# maim and ImageMagick's import both: a grey screen that changes with every input
FAKE_SHOT = """#!{py}
import os, sys, zlib, struct
n = sum(1 for l in open(os.environ["FAKE_LOG"]) if l.split()[0] in ("click", "type", "key"))
level = 10 + (n * 60) % 200      # more than the settle check's tolerance per input
if any("pgm:" in x for x in sys.argv):
    sys.stdout.buffer.write(b"P5\\n16 10\\n255\\n" + bytes([level]) * 160)
    sys.exit(0)
def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
w, h = 1280, 800
raw = (b"\\x00" + bytes([level]) * w) * h
sys.stdout.buffer.write(b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
"""

GUEST_SCRIPT = r'''
import asyncio, json, os, socket, sys, time, types
from backend import display

root = sys.argv[1]
display.APPS_PATH = root + "/apps.json"
display.HOLD_GRACE_S = 0.5
display.IDLE_STOP_S = 600
fakebin = root + "/bin"
orig_env = display._app_env
display._app_env = lambda extra=None: {**orig_env(extra), "FAKE_LOG": root + "/log",
    "PATH": fakebin + ":" + orig_env(extra)["PATH"]}
json.dump({"v": 1, "apps": {"terminal": {"argv": ["xterm"], "env": {}},
                            "browser": {"argv": ["chromium"], "env": {}}}},
          open(display.APPS_PATH, "w"))

async def until(cond, secs=15):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()

def brief(m):
    return {k: v for k, v in (m or {}).items() if k != "image"}

async def frames(loop, sock, buf, want):
    while True:
        while b"\n" in buf[0]:
            line, _, buf[0] = buf[0].partition(b"\n")
            msg = json.loads(line)
            if want(msg):
                return msg
        data = await asyncio.wait_for(loop.sock_recv(sock, 1 << 20), 20)
        if not data:
            return None
        buf[0] += data

async def main():
    loop = asyncio.get_running_loop()
    # 1) no screen: refused, nothing spawned
    a, b = socket.socketpair(); a.setblocking(False); b.setblocking(False)
    t = asyncio.ensure_future(display._handle(loop, b))
    await loop.sock_sendall(a, b'{"mode": "desk"}\n')
    reply = json.loads((await loop.sock_recv(a, 4096)).splitlines()[0])
    assert reply["ok"] is False and "not running" in reply["error"], reply
    await t
    a.close()
    # 2) the screen is up (as far as the listener knows): the seat opens
    display.session._x = types.SimpleNamespace(returncode=None)
    a, b = socket.socketpair(); a.setblocking(False); b.setblocking(False)
    t = asyncio.ensure_future(display._handle(loop, b))
    await loop.sock_sendall(a, b'{"mode": "desk"}\n')
    buf = [b""]
    ack = await frames(loop, a, buf, lambda m: "ok" in m)
    assert ack == {"ok": True}, ack
    hello = await frames(loop, a, buf, lambda m: m.get("type") == "hello")
    assert hello["backend"] == "x11" and hello["monitors"][0]["w"] == 1280, hello
    assert sorted(hello["apps"]) == ["browser", "terminal"], hello["apps"]    # the box's own apps
    assert hello["ceiling"]["shell"] is False and display.session.desks == 1
    assert display.session.status()["desks"] == 1
    assert display.session._holds == 0                      # connected, not yet working
    # a request: the real Session shoots the (fake) screen, and the display is held
    await loop.sock_sendall(a, (json.dumps({"type": "grants", "screen": True, "input": True,
                                            "shell": "off"}) + "\n").encode())
    await loop.sock_sendall(a, (json.dumps({"type": "req", "id": "r1", "verb": "screenshot",
                                            "params": {}}) + "\n").encode())
    res = await frames(loop, a, buf, lambda m: m.get("type") == "res")
    assert res["id"] == "r1" and res["ok"] is True and res["image"]["w"] == 1280, res.get("err")
    assert "no accessibility tree" in res["elements_note"], brief(res)
    assert display.session._holds == 1                      # held while the agent works
    await loop.sock_sendall(a, (json.dumps({"type": "req", "id": "r2", "verb": "click",
                                            "params": {"x": 40, "y": 50}}) + "\n").encode())
    res = await frames(loop, a, buf, lambda m: m.get("type") == "res")
    assert res["ok"] is True and res["changed"] is True, brief(res)
    log = open(root + "/log").read()
    assert "mousemove --sync 40 50" in log and "click" in log, log
    # the grace runs out: released, the idle clock may stop the screen again
    assert await until(lambda: display.session._holds == 0)
    await loop.sock_sendall(a, (json.dumps({"type": "req", "id": "r3", "verb": "screenshot",
                                            "params": {}}) + "\n").encode())
    await frames(loop, a, buf, lambda m: m.get("type") == "res")
    assert display.session._holds == 1
    a.close()                                               # the host hangs up: released at once
    await asyncio.wait_for(t, 10)
    assert display.session._holds == 0 and display.session.desks == 0
    # 3) the display stops under an open seat: the connection ends
    a, b = socket.socketpair(); a.setblocking(False); b.setblocking(False)
    t = asyncio.ensure_future(display._handle(loop, b))
    await loop.sock_sendall(a, b'{"mode": "desk"}\n')
    buf = [b""]
    await frames(loop, a, buf, lambda m: m.get("type") == "hello")
    display.session._x.returncode = 0
    assert await frames(loop, a, buf, lambda m: False) is None     # EOF
    await asyncio.wait_for(t, 10)
    assert display.session.desks == 0
    # 4) a box without xdotool: the child says so instead of a hello
    display.session._x = types.SimpleNamespace(returncode=None)
    os.remove(fakebin + "/xdotool")
    display._app_env = lambda extra=None: {**orig_env(extra), "PATH": "/nonexistent"}
    a, b = socket.socketpair(); a.setblocking(False); b.setblocking(False)
    t = asyncio.ensure_future(display._handle(loop, b))
    await loop.sock_sendall(a, b'{"mode": "desk"}\n')
    buf = [b""]
    err = await frames(loop, a, buf, lambda m: m.get("type") == "error")
    assert "xdotool" in err["error"], err
    a.close()
    await asyncio.wait_for(t, 10)
    print("DESK-GUEST-OK")

asyncio.run(main())
'''


def test_guest_desk_mode_runs_the_client_holds_the_display_and_ends_with_it(tmp_path):
    import shutil
    import tempfile
    short = Path(tempfile.mkdtemp(prefix="d2-", dir="/tmp"))
    try:
        pkg = tmp_path / "pkg"
        with tarfile.open(fileobj=io.BytesIO(build_package_tar()), mode="r:gz") as t:
            t.extractall(pkg, filter="data")
        assert (pkg / "backend" / "jav3_desk.py").is_file()       # the client ships
        assert (pkg / "backend" / "deskbox.py").is_file()
        bin_dir = short / "bin"
        bin_dir.mkdir()
        for name, body in (("xdotool", FAKE_XDOTOOL), ("maim", FAKE_SHOT), ("import", FAKE_SHOT)):
            f = bin_dir / name
            f.write_text(body.format(py=sys.executable))
            f.chmod(f.stat().st_mode | stat.S_IXUSR)
        (short / "log").write_text("")
        (short / "script.py").write_text(GUEST_SCRIPT)
        env = {"PYTHONPATH": str(pkg), "PATH": os.environ.get("PATH", ""),
               "HOME": str(short), "XDG_CONFIG_HOME": str(short / "cfg")}
        r = subprocess.run([sys.executable, str(short / "script.py"), str(short)],
                           cwd=pkg, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and "DESK-GUEST-OK" in r.stdout, \
            r.stdout[-800:] + r.stderr[-2500:]
    finally:
        shutil.rmtree(short, ignore_errors=True)
