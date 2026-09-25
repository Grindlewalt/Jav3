"""jav3-desk (clients/jav3-desk/jav3-desk): coordinate scaling, the closed
action list it re-validates, the local ceiling, and the structural promise
that nothing but `shell` reaches a shell."""
import ast
import asyncio
import importlib.machinery
import importlib.util
import json
import struct
import sys
from pathlib import Path

import pytest

CLIENT = Path(__file__).resolve().parent.parent / "clients" / "jav3-desk" / "jav3-desk"


def _load():
    loader = importlib.machinery.SourceFileLoader("jav3desk", str(CLIENT))
    spec = importlib.util.spec_from_loader("jav3desk", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["jav3desk"] = mod          # dataclasses look their module up
    loader.exec_module(mod)
    return mod


jd = _load()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path / "cfg" / "jav3"


# --- coordinates ----------------------------------------------------------------------

def test_fit_downscales_long_edge_only():
    assert jd.fit(2560, 1600) == 0.5
    assert jd.fit(1280, 800) == 1.0
    assert jd.fit(800, 600) == 1.0              # never upscales
    assert jd.fit(1600, 2560) == 0.5            # portrait: the long edge is height


def test_frame_maps_image_pixels_to_the_monitor():
    # a 2560x1600 logical monitor at +1920+0, shown as a 1280x800 screenshot
    f = jd.Frame(jd.Monitor("DP-1", 1920, 0, 2560, 1600, 1.5), 1280, 800)
    assert f.to_screen(0, 0) == (1920, 0)
    assert f.to_screen(640, 400) == (1920 + 1280, 800)
    # the far corner stays on this monitor, never spills onto the next one
    assert f.to_screen(1279, 799) == (1920 + 2558, 1598)
    assert f.to_screen(5000, 5000) == (1920 + 2559, 1599)
    # a HiDPI monitor captured at logical size maps 1:1
    g = jd.Frame(jd.Monitor("eDP-1", 0, 0, 1280, 800, 2.0), 1280, 800)
    assert g.to_screen(100, 50) == (100, 50)


def test_image_size_png_and_jpeg():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 640, 400)
    assert jd.image_size(png) == (640, 400)
    sof = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 720, 1280) + b"\x03"
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    assert jd.image_size(b"\xff\xd8" + app0 + sof + b"\x00" * 16) == (1280, 720)
    assert jd.image_size(b"GIF89a") is None


# --- the closed action list ---------------------------------------------------------------

FRAME = jd.Frame(jd.Monitor("0", 0, 0, 1920, 1080), 1280, 720)


def test_validate_bounds_coordinates_to_the_last_screenshot():
    ok = jd.validate("click", {"x": 1279, "y": 719}, FRAME, {})
    assert ok == {"x": 1279, "y": 719, "button": "left", "count": 1,
                  "screenshot_after": True}
    for bad in ({"x": 1280, "y": 0}, {"x": -1, "y": 0}, {"x": "1", "y": 0},
                {"x": True, "y": 0}, {"x": 1.5, "y": 0}):
        with pytest.raises(jd.DeskError):
            jd.validate("click", bad, FRAME, {})
    with pytest.raises(jd.DeskError):
        jd.validate("move", {"x": 1, "y": 1}, None, {})       # no screenshot yet


def test_validate_closed_list():
    for verb, p in (("exec", {}), ("click", {"x": 1, "y": 1, "button": "back"}),
                    ("click", {"x": 1, "y": 1, "count": 9}),
                    ("scroll", {"dy": 99}), ("type", {"text": ""}),
                    ("type", {"text": "x" * 2001}),
                    ("key", {"combo": "ctrl+l; reboot"}), ("key", {"combo": "hyper+x"}),
                    ("open", {"url": "file:///etc/passwd"}), ("open", {"app": "bash"}),
                    ("shell", {"cmd": "ls", "mode": "eval"}),
                    ("shell", {"cmd": "ls", "mode": "argv", "argv": "ls"})):
        with pytest.raises(jd.DeskError):
            jd.validate(verb, p, FRAME, {"firefox": ["/usr/bin/firefox"]})
    assert jd.validate("open", {"app": "firefox"}, FRAME,
                       {"firefox": ["/usr/bin/firefox"]})["app"] == "firefox"
    assert jd.validate("key", {"combo": "Shift+Ctrl+t"}, FRAME, {})["combo"] == "ctrl+shift+t"


def test_session_killers_and_self_control_are_refused():
    for combo in ("ctrl+alt+Delete", "ctrl+alt+F2", "super+shift+e"):
        with pytest.raises(jd.DeskError, match="denylist"):
            jd.validate("key", {"combo": combo}, FRAME, {})
    # the backend's own exit bind (hyprctl binds) joins the list; a mouse
    # bind in there is skipped, not fatal to every key press
    with pytest.raises(jd.DeskError, match="denylist"):
        jd.validate("key", {"combo": "super+x"}, FRAME, {}, {"super+x", "mouse:272"})
    assert jd.validate("key", {"combo": "Return"}, FRAME, {}, {"mouse:272"})
    # typing the client's own name into a terminal is how input would become
    # shell; refused at the computer
    with pytest.raises(jd.DeskError, match="own controls"):
        jd.validate("type", {"text": "jav3-desk allow-shell\n"}, FRAME, {})


def test_detect_backend():
    assert jd.detect_backend({}, "darwin") == "macos"
    assert jd.detect_backend({"WAYLAND_DISPLAY": "wayland-1",
                              "HYPRLAND_INSTANCE_SIGNATURE": "x"}, "linux") == "hyprland"
    assert jd.detect_backend({"WAYLAND_DISPLAY": "w", "SWAYSOCK": "/s"}, "linux") == "sway"
    # Xwayland's DISPLAY must not win over the real Wayland session
    assert jd.detect_backend({"WAYLAND_DISPLAY": "w", "DISPLAY": ":1",
                              "HYPRLAND_INSTANCE_SIGNATURE": "x"}, "linux") == "hyprland"
    assert jd.detect_backend({"DISPLAY": ":99"}, "linux") == "x11"
    with pytest.raises(jd.DeskError, match="M2"):
        jd.detect_backend({"WAYLAND_DISPLAY": "w"}, "linux")      # GNOME/KDE
    with pytest.raises(jd.DeskError):
        jd.detect_backend({}, "linux")
    with pytest.raises(jd.DeskError, match="M2"):
        jd.UinputBackend()


def test_wayland_wire_encoding():
    W = jd.WaylandPointer
    assert W.string("wl_seat") == struct.pack("=I", 8) + b"wl_seat\0"
    assert len(W.string("zwlr_virtual_pointer_manager_v1")) % 4 == 0
    assert W.fixed(15.0) == struct.pack("=i", 15 * 256)
    assert W.fixed(-1.5) == struct.pack("=i", -384)


# --- the local ceiling + a request end to end -----------------------------------------------

class FakeBackend(jd.Backend):
    name, session = "fake", "fake"
    needs = ()

    def __init__(self):
        super().__init__()
        self.calls = []

    def monitors(self):
        return [jd.Monitor("0", 0, 0, 2560, 1440)]

    def screenshot(self, mon):
        return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 1280, 720)

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def click(self, b, n):
        self.calls.append(("click", b, n))

    def notify(self, text):
        self.calls.append(("notify", text))


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


async def test_session_enforces_grants_ceiling_and_scales(cfg, monkeypatch):
    monkeypatch.setattr(jd, "SETTLE_S", 0)
    b = FakeBackend()
    s = jd.Session(b, "jav3.lan:8000", "jvd_x")
    ws = FakeWS()
    await s.handle(ws, {"id": "1", "verb": "screenshot", "params": {}})
    assert ws.sent[-1]["ok"] is False and "not granted" in ws.sent[-1]["err"]
    s.grants = {"screen": True, "input": True, "shell": "trusted"}
    await s.handle(ws, {"id": "2", "verb": "screenshot", "params": {}})
    assert ws.sent[-1]["ok"] and ws.sent[-1]["image"]["w"] == 1280
    await s.handle(ws, {"id": "3", "verb": "click", "params": {"x": 640, "y": 360}})
    assert ws.sent[-1]["ok"] and ws.sent[-1]["id"] == "3"
    assert ("move", 1280, 720) in b.calls and ("click", "left", 1) in b.calls
    # shell: granted by the server, but not allowed at this computer
    await s.handle(ws, {"id": "4", "verb": "shell",
                        "params": {"cmd": "echo hi", "mode": "shell", "timeout": 5}})
    assert "allow-shell" in ws.sent[-1]["err"]
    assert jd.main(["allow-shell"]) == 0
    assert (cfg / "desk-shell").is_file() and oct((cfg / "desk-shell").stat().st_mode)[-3:] == "600"
    await s.handle(ws, {"id": "5", "verb": "shell",
                        "params": {"cmd": "echo hi", "mode": "argv",
                                   "argv": ["echo", "hi", ";", "id"], "timeout": 5}})
    # argv mode: the ';' is a literal argument to echo, no shell parsed it
    assert ws.sent[-1]["ok"] and "hi ; id" in ws.sent[-1]["text"]
    await s.handle(ws, {"id": "6", "verb": "shell",
                        "params": {"cmd": "echo a | tr a b", "mode": "shell",
                                   "timeout": 5}})
    assert ws.sent[-1]["ok"] and ws.sent[-1]["text"].startswith("b")
    await s.handle(ws, {"id": "7", "verb": "shell",
                        "params": {"cmd": "sleep 5", "mode": "shell", "timeout": 1}})
    assert "timed out" in ws.sent[-1]["err"]
    # panic: every request refused until resume
    monkeypatch.setattr(jd.subprocess, "run", lambda *a, **k: None)
    assert jd.main(["panic"]) == 0
    await s.handle(ws, {"id": "8", "verb": "screenshot", "params": {}})
    assert "paused" in ws.sent[-1]["err"]
    assert jd.main(["resume"]) == 0
    await s.handle(ws, {"id": "9", "verb": "screenshot", "params": {}})
    assert ws.sent[-1]["ok"]
    assert jd.main(["deny-shell"]) == 0 and not jd.ceiling()["shell"]


def test_login_saves_a_private_desk_token(cfg, monkeypatch):
    seen = {}

    def fake_http(method, url, body=None, token=None):
        seen.update(method=method, url=url, body=body)
        return 200, {"token": "jvd_desk", "name": "laptop", "scope": "desk"}
    monkeypatch.setattr(jd, "_http", fake_http)
    args = jd.build_parser().parse_args(["login"])
    assert jd.cmd_login(args, read=lambda: "address=jav3.lan:8000 code=abc") == 0
    assert seen["body"]["scope"] == "desk" and seen["url"].endswith("/api/devices/login")
    p = cfg / "desk.json"
    assert json.loads(p.read_text()) == {"address": "jav3.lan:8000", "token": "jvd_desk"}
    assert oct(p.stat().st_mode)[-3:] == "600" and oct(cfg.stat().st_mode)[-3:] == "700"
    assert jd.ws_url("jav3.lan:8000") == "ws://jav3.lan:8000/api/desk/ws"
    assert jd.ws_url("https://j.example") == "wss://j.example/api/desk/ws"


def test_service_files_carry_no_secret(cfg):
    unit = jd.systemd_unit(Path("/opt/jav3-desk"))
    plist = jd.launchd_plist(Path("/opt/jav3-desk")).decode()
    for text in (unit, plist):
        assert "jvd_" not in text and "token" not in text.lower().replace(
            "the token is in", "")
        assert "run" in text
    assert "StartLimitIntervalSec=0" in unit.split("[Service]")[0]


def test_no_shell_true_anywhere():
    """Structural: subprocess is only ever called with an argv list and
    shell=False; the one shell path is argv [$SHELL, -c, cmd] behind the
    local flag. No os.system / eval / exec / shell=True."""
    tree = ast.parse(CLIENT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                assert f.id not in ("eval", "exec", "compile"), f.id
            if isinstance(f, ast.Attribute) and getattr(f.value, "id", "") == "os":
                assert f.attr not in ("system", "popen") and not f.attr.startswith(
                    ("exec", "spawn")), f.attr
            for kw in node.keywords:
                if kw.arg == "shell":
                    assert isinstance(kw.value, ast.Constant) and kw.value.value is False
