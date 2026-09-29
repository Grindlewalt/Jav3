"""jav3-desk (clients/jav3-desk/jav3-desk): coordinate scaling, the closed
action list it re-validates, the local ceiling, and the structural promise
that nothing but `shell` reaches a shell."""
import ast
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


async def test_locked_or_asleep_screen_is_refused_and_reported(cfg, monkeypatch):
    monkeypatch.setattr(jd, "SETTLE_S", 0)
    b = FakeBackend()
    st = {"locked": True, "asleep": False}
    b.screen_state = lambda: dict(st)
    s = jd.Session(b, "jav3.lan:8000", "jvd_x")
    h = s.hello()
    assert h["locked"] is True and h["asleep"] is False and h["v"] == 1
    s.grants = {"screen": True, "input": True, "shell": "off"}
    ws = FakeWS()
    await s.handle(ws, {"id": "1", "verb": "screenshot", "params": {}})
    assert ws.sent[-1] == {"type": "res", "id": "1", "ok": False,
                           "err": "the screen is locked — ask the operator to unlock it"}
    st.update(locked=False, asleep=True)
    await s.handle(ws, {"id": "2", "verb": "screenshot", "params": {}})
    # the change is reported as a state frame before the refusal
    assert ws.sent[-2] == {"type": "state", "locked": False, "asleep": True}
    assert ws.sent[-1]["err"] == "the display is asleep — ask the operator to wake it"
    assert not b.calls                                   # no input, no capture
    st.update(asleep=False)
    await s.handle(ws, {"id": "3", "verb": "screenshot", "params": {}})
    assert ws.sent[-1]["ok"]
    # a probe that raises reads as awake: the capture itself will say what broke
    b.screen_state = lambda: 1 / 0
    assert s.state() == {"locked": False, "asleep": False}


def test_monitor_error_names_the_valid_choices(cfg):
    s = jd.Session(FakeBackend(), "jav3.lan:8000", "jvd_x")
    with pytest.raises(jd.DeskError) as e:
        s._pick("7")
    assert str(e.value) == 'no monitor \'7\' (have: 0); use monitor="0" or an index from 1'


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


# --- navigation: zoom, elements, settle, wait/drag (docs/navigation-contract.md A) -----------

def _png(w, h):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", w, h)


def test_frame_region_maps_zoomed_pixels_back_to_the_screen():
    mon = jd.Monitor("DP-1", 1920, 0, 2560, 1440, 1.0)
    full = jd.Frame(mon, 1280, 720)
    rect, echo = jd.region_rect(full, {"x": 100, "y": 50, "w": 320, "h": 180})
    assert rect == (200, 100, 640, 360) and echo == {"x": 100, "y": 50, "w": 320, "h": 180}
    z = jd.Frame(mon, *jd.zoom_size(640, 360), rect=rect, region=echo)
    assert (z.iw, z.ih) == (1280, 720)
    assert z.to_screen(0, 0) == (1920 + 200, 100)
    assert z.to_screen(640, 360) == (1920 + 200 + 320, 100 + 180)
    # clamped to the zoomed rect, never outside it
    assert z.to_screen(1279, 719) == (1920 + 200 + 639, 100 + 359)
    for px, py in ((0, 0), (17, 33), (640, 360), (1000, 700)):
        gx, gy = z.to_screen(px, py)
        bx, by = z.from_screen(gx, gy)
        assert abs(bx - px) <= 1 and abs(by - py) <= 1
    # the same point through the full frame and the zoomed frame agree
    assert full.to_screen(100 + 160, 50 + 90) == z.to_screen(640, 360)
    assert z.wire() == {"monitor": "DP-1", "index": 1, "count": 1,
                        "region": echo, "screen": {"w": 2560, "h": 1440}}


def test_region_is_clipped_to_the_full_frame_and_zoom_is_capped():
    full = jd.Frame(jd.Monitor("0", 0, 0, 1280, 800), 1280, 800)
    rect, echo = jd.region_rect(full, {"x": 1200, "y": 700, "w": 400, "h": 400})
    assert echo == {"x": 1200, "y": 700, "w": 80, "h": 100} and rect == (1200, 700, 80, 100)
    with pytest.raises(jd.DeskError, match="outside"):
        jd.region_rect(full, {"x": 1279, "y": 0, "w": 50, "h": 50})
    assert jd.zoom_size(640, 400) == (1280, 800)
    assert jd.zoom_size(100, 50) == (300, 150)             # upscale capped at 3x
    assert jd.zoom_size(100, 50, 2.0) == (600, 300)        # Retina: 3x of native pixels


def test_build_elements_clips_scales_and_numbers_in_reading_order():
    mon = jd.Monitor("0", 0, 0, 2560, 1600)
    f = jd.Frame(mon, 1280, 800)
    raw = [
        {"role": "button", "label": "Right", "box": (2000, 100, 200, 60)},
        {"role": "button", "label": "Left", "box": (100, 110, 200, 60)},       # same row
        {"role": "textfield", "label": "Search", "box": (100, 400, 600, 50),
         "value": "foo", "focused": True},
        {"role": "button", "label": "", "box": (2500, 1580, 200, 200)},         # clipped
        {"role": "link", "label": "Gone", "box": (3000, 0, 50, 50)},            # outside
        {"role": "listitem", "label": "", "box": (0, 600, 400, 40)},            # empty row
    ]
    els = jd.build_elements(raw, f, "ax")
    assert [e["label"] for e in els] == ["Left", "Right", "Search", ""]
    assert [e["id"] for e in els] == [1, 2, 3, 4]
    assert els[0] == {"id": 1, "role": "button", "label": "Left", "x": 50, "y": 55,
                      "w": 100, "h": 30, "src": "ax"}
    assert els[2]["value"] == "foo" and els[2]["focused"] is True
    assert els[3]["x"] + els[3]["w"] == 1280 and els[3]["y"] + els[3]["h"] == 800
    # in a zoomed frame the same element is bigger and elsewhere; others drop
    z = jd.Frame(mon, 1280, 800, rect=(0, 0, 640, 400))
    els = jd.build_elements(raw, z, "ax")
    assert [e["label"] for e in els] == ["Left"]
    assert (els[0]["x"], els[0]["y"], els[0]["w"], els[0]["h"]) == (200, 220, 400, 120)


def _ax(role, box=None, children=(), **attrs):
    d = {"AXRole": role, "AXChildren": list(children), **attrs}
    if box:
        d["AXPosition"], d["AXSize"] = box[:2], box[2:]
    return d


def test_ax_tree_parser_on_a_recorded_tree():
    """A recorded AX tree (attribute dicts as the ctypes reader produces
    them) through ax_info + walk_tree: roles, labels, static-text labels for
    icon-only controls, structure walked through, off-screen pruned."""
    win = _ax("AXWindow", (0, 0, 1440, 900), [
        _ax("AXToolbar", (0, 0, 1440, 50), [
            _ax("AXButton", (10, 10, 30, 30), AXDescription="Back"),
            _ax("AXButton", (50, 10, 30, 30), [_ax("AXStaticText", AXValue="Share")]),
            _ax("AXButton", (90, 10, 30, 30)),                      # icon-only, no text
            _ax("AXRadioButton", (200, 10, 80, 30), AXSubrole="AXTabButton",
                AXTitle="General"),
            _ax("AXCheckBox", (300, 10, 40, 30), AXSubrole="AXSwitch", AXValue=1.0,
                AXTitle="Wi-Fi", AXEnabled=False),
        ]),
        _ax("AXTextField", (10, 100, 300, 24), AXValue="hello",
            AXPlaceholderValue="Search", AXFocused=True),
        _ax("AXGroup", (5000, 5000, 100, 100), [_ax("AXButton", (5000, 5000, 10, 10),
                                                    AXTitle="Offscreen")]),
        _ax("AXStaticText", (10, 200, 100, 20), AXValue="just text"),
    ])
    raw, partial = jd.walk_tree([win], jd.ax_info, (0, 0, 1440, 900),
                                jd.time.monotonic() + 5)
    assert not partial
    got = [(e["role"], e["label"], e.get("value"), e.get("focused"), e.get("enabled"))
           for e in raw]
    assert got == [("button", "Back", None, None, None),
                   ("button", "Share", None, None, None),
                   ("button", "", None, None, None),
                   ("tab", "General", None, None, None),
                   ("toggle", "Wi-Fi", "1", None, False),
                   ("textfield", "Search", "hello", True, None)]


def test_walk_tree_budget_depth_and_dedupe():
    deep = {"role": "button", "label": "deep", "box": (0, 0, 10, 10), "children": []}
    for _ in range(15):
        deep = {"role": "", "box": None, "children": [deep]}
    dup = {"role": "button", "label": "x", "box": (0, 0, 10, 10), "children": [
        {"role": "button", "label": "x", "box": (0, 0, 10, 10), "children": []}]}
    raw, _ = jd.walk_tree([deep, dup], lambda n: n, (0, 0, 100, 100),
                          jd.time.monotonic() + 5)
    assert [e["label"] for e in raw] == ["x"]              # depth cap 12; one dedupe
    raw, partial = jd.walk_tree([dup], lambda n: n, (0, 0, 100, 100),
                                jd.time.monotonic() - 1)
    assert raw == [] and partial                           # budget spent: partial
    hidden = {"role": "", "hidden": True, "children": [dup]}
    assert jd.walk_tree([hidden], lambda n: n, (0, 0, 100, 100),
                        jd.time.monotonic() + 5)[0] == []


def test_atspi_parser_on_a_recorded_tree():
    def node(role, name="", states=("showing",), ext=None, kids=()):
        return (role, name, "", set(states), ext, list(kids))

    tree = node("frame", "Files", ("showing", "active"), (0, 0, 800, 600), [
        node("push button", "Open", ("showing", "enabled"), (10, 10, 60, 24)),
        node("text", "", ("showing", "editable", "focused"), (80, 10, 200, 24)),
        node("text", "a label", ("showing",), (80, 40, 200, 24)),
        node("table cell", "row", ("showing",), (0, 100, 800, 20)),
        node("table cell", "sel", ("showing", "selectable"), (0, 120, 800, 20)),
        node("check box", "Hidden", ("enabled",), (0, 140, 20, 20)),
        node("toggle button", "", ("showing", "checked"), (0, 160, 20, 20),
             [node("label", "Bold", ("showing",), (0, 160, 20, 20))]),
    ])
    raw, _ = jd.walk_tree([tree], lambda n: jd.atspi_info(*n), (0, 0, 800, 600),
                          jd.time.monotonic() + 5)
    assert [(e["role"], e["label"], e.get("value")) for e in raw] == [
        ("button", "Open", None), ("textfield", "", None), ("cell", "sel", None),
        ("toggle", "Bold", "checked")]
    assert raw[1]["focused"] is True and "enabled" not in raw[0]


def test_validate_new_verbs_and_screenshot_params():
    ok = jd.validate("drag", {"x": 0, "y": 0, "to_x": 1279, "to_y": 719}, FRAME, {})
    assert ok == {"x": 0, "y": 0, "to_x": 1279, "to_y": 719, "button": "left",
                  "screenshot_after": True}
    for bad in ({"x": 0, "y": 0, "to_x": 1280, "to_y": 0}, {"x": 0, "y": 0, "to_x": 1},
                {"x": 0, "y": 0, "to_x": 1, "to_y": 1, "button": "back"}):
        with pytest.raises(jd.DeskError):
            jd.validate("drag", bad, FRAME, {})
    with pytest.raises(jd.DeskError):
        jd.validate("drag", {"x": 0, "y": 0, "to_x": 1, "to_y": 1}, None, {})
    assert jd.validate("wait", {}, None, {}) == {"mode": "stable", "timeout_ms": 3000}
    assert jd.validate("wait", {"mode": "change", "timeout_ms": 10000}, None, {})
    for bad in ({"mode": "forever"}, {"timeout_ms": 10001}, {"timeout_ms": -1},
                {"timeout_ms": "5"}):
        with pytest.raises(jd.DeskError):
            jd.validate("wait", bad, None, {})
    assert jd.validate("screenshot", {}, None, {}) == {"elements": True, "walk": "front"}
    assert jd.validate("screenshot", {"walk": "all"}, None, {})["walk"] == "all"
    assert jd.validate("screenshot", {"walk": "rm -rf"}, None, {})["walk"] == "front"
    got = jd.validate("screenshot", {"region": {"x": 1, "y": 2, "w": 30, "h": 40},
                                     "elements": False, "monitor": 1}, None, {})
    assert got == {"monitor": "1", "region": {"x": 1, "y": 2, "w": 30, "h": 40},
                   "elements": False, "walk": "front"}
    for bad in ({"x": 1, "y": 2, "w": 3, "h": 40}, {"x": -1, "y": 0, "w": 9, "h": 9},
                [1, 2, 3, 4], {"x": 1, "y": 2, "w": 30}):
        with pytest.raises(jd.DeskError):
            jd.validate("screenshot", {"region": bad}, None, {})


def _pgm(pixels, w=8, h=4):
    return b"P5\n%d %d\n255\n" % (w, h) + bytes(pixels)


def test_thumbnails_decode_and_tolerate_a_caret():
    base = [100] * 32
    assert jd.decode_thumb(_pgm(base)) == (8, 4, bytes(base))
    caret = list(base)
    caret[5] = 250
    assert jd.thumbs_match(_pgm(base), _pgm(caret))          # one pixel: same screen
    moved = [250] * 8 + [100] * 24
    assert not jd.thumbs_match(_pgm(base), _pgm(moved))
    assert not jd.thumbs_match(None, _pgm(base))
    assert jd.thumbs_match(b"jpegbytes", b"jpegbytes") and not jd.thumbs_match(b"a", b"b")
    # P6 (grim) and 24-bit BMPs (sips), top-down and bottom-up, decode to grey
    assert jd.decode_thumb(b"P6 2 1 255\n" + bytes([0, 0, 0, 255, 255, 255])) == \
        (2, 1, bytes([0, 255]))
    hdr = struct.pack("<2sIHHI", b"BM", 0, 0, 0, 54) + struct.pack(
        "<IiiHHIIiiII", 40, 2, -2, 1, 24, 0, 0, 0, 0, 0, 0)
    rows = bytes([0, 0, 0, 255, 255, 255, 0, 0]) + bytes([9, 9, 9, 9, 9, 9, 0, 0])
    assert jd.decode_thumb(hdr + rows) == (2, 2, bytes([0, 255, 9, 9]))
    bottom_up = hdr[:22] + struct.pack("<i", 2) + hdr[26:]
    assert jd.decode_thumb(bottom_up + rows) == (2, 2, bytes([9, 9, 0, 255]))


class NavBackend(jd.Backend):
    """Zoom-aware fake: images sized like the real backends', thumbnails
    from a scripted sequence, a recorded element tree."""
    name, session = "nav", "nav"
    needs = ()

    def __init__(self, thumbs=None, tree=None):
        super().__init__()
        self.calls, self.thumbs = [], list(thumbs or [])
        self.last_thumb = b"T0"
        self.tree = tree or []

    def monitors(self):
        return [jd.Monitor("DP-1", 0, 0, 2560, 1600),
                jd.Monitor("HDMI-A-1", 2560, 0, 1920, 1080)]

    def screenshot(self, mon, rect=None):
        self.calls.append(("shot", mon.name, rect))
        if rect is None:
            s = jd.fit(mon.w, mon.h)
            return _png(round(mon.w * s), round(mon.h * s))
        return _png(*jd.zoom_size(rect[2], rect[3]))

    def thumbnail(self, mon, rect=None):
        if self.thumbs:
            self.last_thumb = self.thumbs.pop(0)
        return self.last_thumb

    def elements_source(self):
        return jd.FakeElementSource(self.tree)

    def cursor(self):
        return (400, 300)

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def click(self, b, n):
        self.calls.append(("click", b, n))

    def drag(self, x0, y0, x1, y1, button):
        self.calls.append(("drag", x0, y0, x1, y1, button))

    def type_text(self, text):
        self.calls.append(("type", text))

    def notify(self, text):
        pass


TREE = [{"role": "", "box": (0, 0, 2560, 1600), "children": [
    {"role": "button", "label": "Save", "box": (400, 300, 200, 60)},
    {"role": "link", "label": "Help", "box": (2000, 1400, 100, 40)}]}]


async def test_screenshot_carries_elements_frame_and_zoom(cfg, monkeypatch):
    monkeypatch.setattr(jd, "SETTLE_S", 0)
    monkeypatch.setattr(jd, "SETTLE_POLL_S", 0)
    b = NavBackend(tree=TREE)
    s = jd.Session(b, "jav3.lan:8000", "jvd_x")
    s.grants = {"screen": True, "input": False, "shell": "off"}
    ws = FakeWS()
    await s.handle(ws, {"id": "1", "verb": "screenshot",
                        "params": {"region": {"x": 0, "y": 0, "w": 100, "h": 100}}})
    assert "full screenshot" in ws.sent[-1]["err"]         # zoom needs a full frame first
    await s.handle(ws, {"id": "2", "verb": "screenshot", "params": {}})
    r = ws.sent[-1]
    assert r["ok"] and (r["image"]["w"], r["image"]["h"]) == (1280, 800)
    assert r["frame"] == {"monitor": "DP-1", "index": 1, "count": 2, "region": None,
                          "screen": {"w": 2560, "h": 1600}}
    assert r["elements_src"] == "ax" and "elements_note" not in r
    assert [(e["id"], e["label"], e["x"], e["y"], e["w"], e["h"]) for e in r["elements"]] \
        == [(1, "Save", 200, 150, 100, 30), (2, "Help", 1000, 700, 50, 20)]
    assert r["cursor"] == {"x": 200, "y": 150}
    # zoom on the Save button: region in full-frame px, elements in zoomed px
    await s.handle(ws, {"id": "3", "verb": "screenshot",
                        "params": {"region": {"x": 160, "y": 100, "w": 320, "h": 200}}})
    r = ws.sent[-1]
    assert r["ok"] and r["frame"]["region"] == {"x": 160, "y": 100, "w": 320, "h": 200}
    assert b.calls[-1] == ("shot", "DP-1", (320, 200, 640, 400))
    assert (r["image"]["w"], r["image"]["h"]) == (1280, 800)
    assert [(e["label"], e["x"], e["y"], e["w"], e["h"]) for e in r["elements"]] == \
        [("Save", 160, 200, 400, 120)]
    # the server bounds-checks against THIS image; a click lands in the zoom
    s.grants["input"] = True
    await s.handle(ws, {"id": "4", "verb": "click", "params": {"x": 360, "y": 260}})
    r = ws.sent[-1]
    assert r["ok"] and ("move", 320 + 180, 200 + 130) in b.calls
    assert r["frame"]["region"] == {"x": 160, "y": 100, "w": 320, "h": 200}   # zoom kept
    # elements=false skips the walk and says so
    await s.handle(ws, {"id": "5", "verb": "screenshot",
                        "params": {"monitor": "HDMI-A-1", "elements": False}})
    r = ws.sent[-1]
    assert r["elements"] == [] and r["elements_src"] == "none"
    assert r["frame"]["index"] == 2 and "cursor" not in r    # the cursor is on DP-1


async def test_elements_note_when_the_tree_is_unavailable(cfg):
    b = NavBackend()
    b.elements_source = lambda: jd.ElementSource("no Accessibility permission")
    s = jd.Session(b, "a", "t")
    s.grants = {"screen": True, "input": True, "shell": "off"}
    ws = FakeWS()
    await s.handle(ws, {"id": "1", "verb": "screenshot", "params": {}})
    r = ws.sent[-1]
    assert r["elements"] == [] and r["elements_src"] == "none"
    assert r["elements_note"] == "no Accessibility permission"
    # the plain FakeBackend (no source of its own) says why too
    s2 = jd.Session(FakeBackend(), "a", "t")
    s2.grants = s.grants
    await s2.handle(ws, {"id": "2", "verb": "screenshot", "params": {}})
    assert ws.sent[-1]["elements_note"] == "not supported on this backend"


async def test_settle_reports_changed_and_settled_ms(cfg, monkeypatch):
    monkeypatch.setattr(jd, "SETTLE_S", 0)
    monkeypatch.setattr(jd, "SETTLE_POLL_S", 0)
    # before-thumb A; the click changes it: B, C (still moving), C (stable)
    b = NavBackend(thumbs=[b"A", b"B", b"C", b"C"])
    s = jd.Session(b, "a", "t")
    s.grants = {"screen": True, "input": True, "shell": "off"}
    ws = FakeWS()
    await s.handle(ws, {"id": "0", "verb": "screenshot", "params": {"elements": False}})
    await s.handle(ws, {"id": "1", "verb": "click", "params": {"x": 10, "y": 10}})
    r = ws.sent[-1]
    assert r["ok"] and r["changed"] is True and isinstance(r["settled_ms"], int)
    assert b.thumbs == [] and "image" in r
    # nothing moves: changed false
    b.thumbs = [b"C", b"C", b"C"]
    await s.handle(ws, {"id": "2", "verb": "click",
                        "params": {"x": 10, "y": 10, "screenshot_after": False}})
    r = ws.sent[-1]
    assert r["changed"] is False and "image" not in r
    # a screen that never settles stops at the ceiling
    monkeypatch.setattr(jd, "SETTLE_MAX_S", 0.05)
    b.thumbs = [struct.pack(">I", i) for i in range(100_000)]
    await s.handle(ws, {"id": "3", "verb": "click", "params": {"x": 10, "y": 10}})
    assert ws.sent[-1]["changed"] is True and ws.sent[-1]["settled_ms"] >= 50


def test_a_dim_overlay_is_a_change_and_a_caret_is_not():
    """The trial's miss: Spotlight's translucent bar over a dark window moved
    many thumbnail pixels by 8-24 grey levels and almost none by more."""
    w, h = 160, 100
    base = [30] * (w * h)
    overlay = list(base)
    for y in range(18, 24):
        for x in range(50, 120):
            overlay[y * w + x] = 30 + 14             # dim bar: +14 levels
    assert not jd.thumbs_match(_pgm(base, w, h), _pgm(overlay, w, h))
    caret = list(base)
    for y in range(40, 43):
        caret[y * w + 80] = 200                       # a caret, 3 px tall
    assert jd.thumbs_match(_pgm(base, w, h), _pgm(caret, w, h))
    noise = [30 + (i % 3) * 3 for i in range(w * h)]  # JPEG-ish wobble under tolerance
    assert jd.thumbs_match(_pgm(base, w, h), _pgm(noise, w, h))


def test_elements_sig_ignores_order_and_position():
    a = [{"role": "button", "label": "Save", "x": 1}, {"role": "tab", "label": "One", "x": 5}]
    b = [{"role": "tab", "label": "One", "x": 9}, {"role": "button", "label": "Save", "x": 2}]
    assert jd.elements_sig(a) == jd.elements_sig(b)
    assert jd.elements_sig(a) != jd.elements_sig(a + [{"role": "textfield", "label": "S"}])
    assert jd.elements_sig(a) != jd.elements_sig([{**a[0], "label": "Saved"}, a[1]])


async def test_changed_uses_the_settled_frame_and_the_element_list(cfg, monkeypatch):
    monkeypatch.setattr(jd, "SETTLE_S", 0)
    monkeypatch.setattr(jd, "SETTLE_POLL_S", 0)
    tree = [{"role": "", "box": (0, 0, 2560, 1600), "children": [
        {"role": "button", "label": "Save", "box": (400, 300, 200, 60)}]}]
    b = NavBackend(tree=tree)
    s = jd.Session(b, "a", "t")
    s.grants = {"screen": True, "input": True, "shell": "off"}
    ws = FakeWS()
    await s.handle(ws, {"id": "0", "verb": "screenshot", "params": {}})
    # the key opens an overlay whose pixels compare equal, but a field appears
    b.key = lambda combo: tree[0]["children"].append(
        {"role": "textfield", "label": "Spotlight Search", "box": (800, 300, 900, 60)})
    await s.handle(ws, {"id": "1", "verb": "key", "params": {"combo": "super+space"}})
    r = ws.sent[-1]
    assert r["ok"] and r["pixels_changed"] is False and r["elements_changed"] is True
    assert r["changed"] is True
    assert any(e["label"] == "Spotlight Search" for e in r["elements"])
    # nothing moves at all: both signals say no
    b.key = lambda combo: None
    await s.handle(ws, {"id": "2", "verb": "key", "params": {"combo": "Escape"}})
    r = ws.sent[-1]
    assert r["changed"] is False and r["elements_changed"] is False
    # the pre-action thumbnail is the one right before the action (when the
    # last response is too old to reuse); the comparison is against the
    # settled frame, not the first capture
    monkeypatch.setattr(jd, "PRE_REUSE_S", 0)
    b.thumbs = [b"PRE", b"MID", b"END", b"END"]
    b.key = lambda combo: None
    await s.handle(ws, {"id": "3", "verb": "key",
                        "params": {"combo": "Escape", "screenshot_after": False}})
    r = ws.sent[-1]
    assert r["pixels_changed"] is True and r["changed"] is True and "image" not in r
    assert "elements_changed" in r                   # the walk ran without a shot too


class CountingSource(jd.FakeElementSource):
    def __init__(self, roots):
        super().__init__(roots)
        self.walks = 0

    def collect(self, clip, deadline, walk="front", windows=None):
        self.walks += 1
        return super().collect(clip, deadline, walk, windows)


async def test_an_action_reuses_the_last_responses_walk_and_thumbnail(cfg, monkeypatch):
    monkeypatch.setattr(jd, "SETTLE_S", 0)
    monkeypatch.setattr(jd, "SETTLE_POLL_S", 0)
    tree = [{"role": "", "box": (0, 0, 2560, 1600), "children": [
        {"role": "button", "label": "Save", "box": (400, 300, 200, 60)}]}]
    src = CountingSource(tree)
    b = NavBackend(tree=tree)
    b.elements_source = lambda: src
    b.cheap_thumb = True
    thumbs = []
    real_thumb = b.thumbnail
    b.thumbnail = lambda mon, rect=None: thumbs.append(rect) or real_thumb(mon, rect)
    s = jd.Session(b, "a", "t")
    s.grants = {"screen": True, "input": True, "shell": "off"}
    ws = FakeWS()
    await s.handle(ws, {"id": "0", "verb": "screenshot", "params": {}})
    assert (src.walks, len(thumbs)) == (1, 1)           # the cheap thumbnail rides along
    r = ws.sent[-1]
    assert set(r["timing"]) == {"capture_ms", "settle_ms", "elements_ms", "total_ms"}
    # a click right after: no walk and no capture before it, one walk after
    b.thumbs = [b"A", b"A"]
    await s.handle(ws, {"id": "1", "verb": "click", "params": {"x": 10, "y": 10}})
    r = ws.sent[-1]
    assert src.walks == 2 and len(thumbs) == 1 + 2      # settle's two captures only
    assert r["elements_changed"] is False and r["pixels_changed"] is True   # T0 -> A
    assert r["timing"]["total_ms"] >= r["timing"]["settle_ms"]
    # chained: the click's settled thumbnail is the next action's before
    b.thumbs = [b"A", b"A"]
    tree[0]["children"].append({"role": "button", "label": "New", "box": (0, 0, 9, 9)})
    await s.handle(ws, {"id": "2", "verb": "click",
                        "params": {"x": 10, "y": 10, "screenshot_after": False}})
    r = ws.sent[-1]
    assert src.walks == 3 and r["pixels_changed"] is False and r["elements_changed"] is True
    # too old: walk and capture again before the action
    monkeypatch.setattr(jd, "PRE_REUSE_S", 0)
    b.thumbs = [b"A", b"A", b"A"]
    await s.handle(ws, {"id": "3", "verb": "click", "params": {"x": 10, "y": 10}})
    assert src.walks == 3 + 2 and ws.sent[-1]["changed"] is False
    # another frame (a zoom) never reuses the full frame's state
    monkeypatch.setattr(jd, "PRE_REUSE_S", 2.0)
    await s.handle(ws, {"id": "4", "verb": "screenshot",
                        "params": {"region": {"x": 0, "y": 0, "w": 640, "h": 400}}})
    s.pre["key"] = ("DP-1", None)
    n = src.walks
    await s.handle(ws, {"id": "5", "verb": "click", "params": {"x": 10, "y": 10}})
    assert src.walks == n + 2


def test_elements_are_grouped_by_window_then_reading_order():
    """The trial's frame: the menu bar (y 0-21) and Brave's tab strip (y 8-43)
    share the first 40-px band and used to interleave."""
    frame = jd.Frame(jd.Monitor("main", 0, 0, 1280, 800), 1280, 800)
    brave = {"role": "", "window": "Brave: benchmarks", "box": (0, 0, 1280, 800),
             "children": [
                 {"role": "tab", "label": "Docs", "box": (153, 8, 189, 35)},
                 {"role": "button", "label": "Close", "box": (17, 8, 14, 14)},
                 {"role": "button", "label": "Reload", "box": (71, 40, 25, 25)}]}
    menu = {"role": "", "window": "menu bar", "box": (0, 0, 1280, 24), "children": [
        {"role": "menuitem", "label": "File", "box": (108, 0, 37, 21)},
        {"role": "menuitem", "label": "Apple", "box": (24, 0, 30, 21)}]}
    spot = {"role": "", "window": "Spotlight", "box": (390, 160, 500, 40), "children": [
        {"role": "textfield", "label": "Spotlight Search", "box": (400, 170, 480, 29)}]}
    raw, partial = jd.FakeElementSource([brave, menu, spot]).collect(
        (0, 0, 1280, 800), 1e18)
    els = jd.build_elements(raw, frame, "ax")
    assert [(e["id"], e["window"], e["label"]) for e in els] == [
        (1, "menu bar", "Apple"), (2, "menu bar", "File"),
        (3, "Brave: benchmarks", "Close"), (4, "Brave: benchmarks", "Docs"),
        (5, "Brave: benchmarks", "Reload"), (6, "Spotlight", "Spotlight Search")]
    # explicit group names (the macOS source) win; AT-SPI names roots by label
    raw, _ = jd.walk_tree([{"role": "", "label": "Editor", "box": (0, 0, 9, 9),
                            "children": [{"role": "button", "label": "OK",
                                          "box": (1, 1, 5, 5)}]}],
                          lambda n: n, (0, 0, 100, 100), 1e18, name_roots=True)
    assert raw[0]["window"] == "Editor"
    raw, _ = jd.walk_tree([brave], lambda n: n, (0, 0, 1280, 800), 1e18,
                          groups=["Brave: other title"])
    assert {e["window"] for e in raw} == {"Brave: other title"}
    # a tree without window names keeps plain reading order and no key
    raw, _ = jd.FakeElementSource(TREE).collect((0, 0, 2560, 1600), 1e18)
    assert all("window" not in e for e in raw)


def _three_windows():
    """The third trial's screen, recorded: TextEdit in front, Discord behind
    it but sticking out (3 buttons, 2 fields, 36 channel rows), a Finder
    window entirely under TextEdit's, and the menu bar."""
    textedit = {"role": "", "kind": "front", "window": "TextEdit: Untitled",
                "box": (100, 100, 1200, 800), "children": [
                    {"role": "button", "label": "New Document", "box": (200, 700, 120, 30)},
                    {"role": "textfield", "label": "", "box": (120, 140, 1100, 500)}]}
    discord = {"role": "", "kind": "background", "window": "Discord: Switch Device",
               "box": (900, 50, 1600, 1200), "children": [
                   {"role": "button", "label": "", "box": (914, 60, 14, 14)},
                   {"role": "button", "label": "", "box": (932, 60, 14, 14)},
                   {"role": "button", "label": "Zoom", "box": (950, 60, 14, 14)},
                   {"role": "textfield", "label": "Search", "box": (1400, 60, 300, 24)},
                   {"role": "textfield", "label": "Message", "box": (1400, 1200, 900, 40)},
                   {"role": "", "box": (900, 100, 300, 1100), "children": [
                       {"role": "listitem", "label": f"#channel-{i}",
                        "box": (910, 110 + i * 28, 280, 26)} for i in range(36)]}]}
    finder = {"role": "", "kind": "background", "window": "Finder: benchmark-game",
              "box": (300, 200, 600, 400), "children": [
                  {"role": "button", "label": "Back", "box": (310, 210, 20, 20)},
                  {"role": "listitem", "label": "notes.txt", "box": (320, 300, 400, 20)}]}
    menu = {"role": "", "kind": "menu", "window": "menu bar", "box": (0, 0, 2560, 24),
            "children": [{"role": "menuitem", "label": "File", "box": (108, 0, 37, 21)}]}
    return [textedit, menu, discord, finder]


def test_background_windows_are_capped_and_covered_ones_dropped():
    roots = _three_windows()
    wins: dict = {}
    raw, partial = jd.FakeElementSource(roots).collect((0, 0, 2560, 1600), 1e18,
                                                       "front", wins)
    by = {}
    for e in raw:
        by.setdefault(e["window"], []).append(e["label"])
    assert not partial
    assert by["TextEdit: Untitled"] == ["New Document", ""]       # front: all of it
    assert by["menu bar"] == ["File"]
    assert by["Discord: Switch Device"] == ["", "", "Zoom", "Search", "Message"]
    assert "Finder: benchmark-game" not in by                     # covered: nothing
    assert wins == {"Discord: Switch Device": {"shown": 5, "total": 41, "more": False}}
    # at most BG_SHOWN, buttons and fields only
    roots[2]["children"][:0] = [{"role": "button", "label": f"b{i}",
                                 "box": (1000 + i * 20, 70, 16, 16)} for i in range(10)]
    wins = {}
    raw, _ = jd.FakeElementSource(roots).collect((0, 0, 2560, 1600), 1e18, "front", wins)
    assert len([e for e in raw if e["window"].startswith("Discord")]) == jd.BG_SHOWN
    assert wins["Discord: Switch Device"]["total"] == 51
    # walk="all": every window in full, no counts
    wins = {}
    raw, _ = jd.FakeElementSource(roots).collect((0, 0, 2560, 1600), 1e18, "all", wins)
    assert len([e for e in raw if e["window"].startswith("Discord")]) == 51
    assert any(e["window"].startswith("Finder") for e in raw) and wins == {}


async def test_screenshot_walk_front_or_all_reports_background_counts(cfg):
    b = NavBackend(tree=_three_windows())
    s = jd.Session(b, "a", "t")
    s.grants = {"screen": True, "input": False, "shell": "off"}
    ws = FakeWS()
    await s.handle(ws, {"id": "1", "verb": "screenshot", "params": {}})
    r = ws.sent[-1]
    assert r["windows"] == [{"window": "Discord: Switch Device", "background": True,
                             "shown": 5, "total": 41}]
    assert len(r["elements"]) == 2 + 1 + 5
    await s.handle(ws, {"id": "2", "verb": "screenshot", "params": {"walk": "all"}})
    r = ws.sent[-1]
    assert "windows" not in r and len(r["elements"]) == 2 + 1 + 41 + 1 + 1


async def test_wait_and_drag(cfg, monkeypatch):
    monkeypatch.setattr(jd, "SETTLE_S", 0)
    monkeypatch.setattr(jd, "SETTLE_POLL_S", 0)
    b = NavBackend(thumbs=[b"A", b"A", b"A", b"B", b"B"], tree=TREE)
    s = jd.Session(b, "a", "t")
    s.grants = {"screen": True, "input": False, "shell": "off"}
    ws = FakeWS()
    await s.handle(ws, {"id": "0", "verb": "screenshot", "params": {}})
    # wait is a screen verb: no input grant needed
    await s.handle(ws, {"id": "1", "verb": "wait",
                        "params": {"mode": "change", "timeout_ms": 2000}})
    r = ws.sent[-1]
    assert r["ok"] and r["changed"] is True and "image" in r and r["elements"]
    b.thumbs = [b"B", b"B"]
    await s.handle(ws, {"id": "2", "verb": "wait", "params": {"mode": "stable"}})
    assert ws.sent[-1]["changed"] is False
    await s.handle(ws, {"id": "3", "verb": "drag",
                        "params": {"x": 100, "y": 100, "to_x": 300, "to_y": 200}})
    assert "input is not granted" in ws.sent[-1]["err"]
    s.grants["input"] = True
    await s.handle(ws, {"id": "4", "verb": "drag",
                        "params": {"x": 100, "y": 100, "to_x": 300, "to_y": 200}})
    assert ws.sent[-1]["ok"] and ("drag", 200, 200, 600, 400, "left") in b.calls


def test_mac_apps_come_from_the_installed_bundles(cfg, tmp_path):
    apps_dir, sys_dir, util_dir = (tmp_path / n for n in ("A", "S", "U"))
    for d, names in ((apps_dir, ["TextEdit.app", "Brave Browser.app", "iTerm.app",
                                 "Adobe (Beta).app", "notes.txt", ".Hidden.app",
                                 "Nested"]),
                     (sys_dir, ["Notes.app", "textedit.app", "Script Editor.app"]),
                     (util_dir, ["Terminal.app", "Disk Utility.app"])):
        d.mkdir()
        for n in names:
            (d / n).mkdir()
    (apps_dir / "Nested" / "Deep.app").mkdir()           # one level only
    dirs = (str(apps_dir), str(sys_dir), str(util_dir), str(tmp_path / "missing"))
    apps = jd.local_apps("darwin", mac_dirs=dirs)
    # sorted, one per name (case-insensitive), no terminal / script runner,
    # no name the server would reject, nothing nested
    assert list(apps) == ["Brave Browser", "Disk Utility", "Notes", "TextEdit"]
    assert apps["TextEdit"] == ["/usr/bin/open", "-a", "TextEdit"]
    for i in range(250):
        (apps_dir / f"App{i:03}.app").mkdir()
    assert len(jd.mac_bundles(dirs)) == jd.APPS_CAP
    # launched only by a name in the list: exact or case-insensitive
    assert jd.app_named(apps, "textedit") == "TextEdit"
    assert jd.app_named(apps, "TextEdit.app") is None and jd.app_named(apps, "") is None
    frame = None
    assert jd.validate("open", {"app": "notes"}, frame, apps)["app"] == "Notes"
    with pytest.raises(jd.DeskError):
        jd.validate("open", {"app": "Terminal"}, frame, apps)
    # the operator's desk-apps.json still wins, as is
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "desk-apps.json").write_text(json.dumps({"Mine": ["/usr/bin/true"],
                                                    "bad;name": ["x"]}))
    assert jd.local_apps("darwin", mac_dirs=dirs) == {"Mine": ["/usr/bin/true"]}


def test_linux_apps_add_desktop_entries(cfg, tmp_path, monkeypatch):
    d = tmp_path / "apps"
    d.mkdir()
    (d / "org.gnome.TextEditor.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Text Editor\nExec=gnome-text-editor\n"
        "[Desktop Action new]\nName=New Window\n")
    (d / "xterm.desktop").write_text("[Desktop Entry]\nName=XTerm\nTerminal=false\n"
                                     "Categories=System;TerminalEmulator;\n")
    (d / "hidden.desktop").write_text("[Desktop Entry]\nName=Hidden\nNoDisplay=true\n")
    (d / "tui.desktop").write_text("[Desktop Entry]\nName=htop\nTerminal=true\n")
    monkeypatch.setattr(jd, "find_bin", lambda n: "/usr/bin/" + n
                        if n in ("firefox", "gtk-launch") else None)
    apps = jd.local_apps("linux", linux_dirs=(str(d),))
    assert apps == {"firefox": ["/usr/bin/firefox"],
                    "Text Editor": ["/usr/bin/gtk-launch", "org.gnome.TextEditor"]}
    monkeypatch.setattr(jd, "find_bin", lambda n: None)     # no gtk-launch: PATH only
    assert jd.local_apps("linux", linux_dirs=(str(d),)) == {}


def test_typed_check_reads_the_focused_field_back():
    field = {"role": "textfield", "label": "Spotlight Search", "value": ""}
    assert jd.typed_check("TextEdit", field) == (
        'typed text did not appear in the focused field ("textfield Spotlight Search")'
        " — click the field first, then type")
    assert jd.typed_check("TextEdit", {**field, "value": "Open textedit"}) is None
    assert jd.typed_check("TextEdit", {**field, "value": None,
                                       "selected": "TextEdit"}) is None
    long = "word " * 100
    assert jd.typed_check(long, {**field, "value": "x " + long.strip()}) is None
    assert "nothing on this computer has keyboard focus" in jd.typed_check("a", {})
    # no focus reported but the screen changed (Chromium, canvas editors): no claim
    assert jd.typed_check("a", {}, changed=True) is None
    assert jd.typed_check("a", {"role": "", "label": "", "value": None}, True) is None
    assert jd.typed_check("TextEdit", field, changed=True) is not None   # value is proof
    assert 'not a text field ("button Save")' in jd.typed_check(
        "a", {"role": "button", "label": "Save", "value": None})
    # cannot tell: no reader, a secure field, a newline that may have submitted
    assert jd.typed_check("a", None) is None
    assert jd.typed_check("pw", {**field, "secure": True}) is None
    assert jd.typed_check("hi\n", field) is None
    # the error names the field, never its value
    assert "hunter2" not in jd.typed_check("x", {**field, "value": "hunter2"})


async def test_type_is_read_back_and_fails_with_the_screen(cfg, monkeypatch):
    monkeypatch.setattr(jd, "SETTLE_S", 0)
    monkeypatch.setattr(jd, "SETTLE_POLL_S", 0)
    b = NavBackend(tree=TREE)
    focus = {"role": "textfield", "label": "Search", "value": ""}
    b.elements_source = lambda: jd.FakeElementSource(TREE, focus=lambda: focus)
    s = jd.Session(b, "a", "t")
    s.grants = {"screen": True, "input": True, "shell": "off"}
    ws = FakeWS()
    await s.handle(ws, {"id": "0", "verb": "screenshot", "params": {}})
    await s.handle(ws, {"id": "1", "verb": "type",
                        "params": {"text": "TextEdit", "screenshot_after": False}})
    r = ws.sent[-1]
    assert r["ok"] is False and r["err"].startswith("typed text did not appear in the "
                                                    'focused field ("textfield Search")')
    assert "image" in r and r["elements"]          # the screen comes back anyway
    assert ("type", "TextEdit") in b.calls
    focus["value"] = "TextEdit"
    await s.handle(ws, {"id": "2", "verb": "type",
                        "params": {"text": "TextEdit", "screenshot_after": False}})
    assert ws.sent[-1]["ok"] is True and "image" not in ws.sent[-1]
    # a backend that cannot read focus never blocks typing
    s2 = jd.Session(NavBackend(tree=TREE), "a", "t")
    s2.grants = s.grants
    await s2.handle(ws, {"id": "3", "verb": "screenshot", "params": {}})
    await s2.handle(ws, {"id": "4", "verb": "type", "params": {"text": "x"}})
    assert ws.sent[-1]["ok"] is True


def test_drag_path_moves_in_steps_and_ends_on_target():
    p = jd.drag_path(0, 0, 120, 60)
    assert p[-1] == (120, 60) and len(p) == 12 and p[0] == (10, 5)
    short = jd.drag_path(5, 5, 6, 5)
    assert short[-1] == (6, 5) and len(set(short)) == len(short)


# --- macOS in-process capture (native calls mocked) ---------------------------------------

from types import SimpleNamespace as _NS
from unittest import mock as _mock

_mac = pytest.mark.skipif(sys.platform != "darwin", reason="macOS capture path")


def _mac_backend(native=(2560, 1600), screen=None, access=True):
    b = object.__new__(jd.MacBackend)
    b.cheap_thumb = True
    b._dids = {"main": 7}
    b.Pt = lambda x, y: _NS(x=x, y=y)
    b.CGRect = lambda o, s: (o.x, o.y, s.x, s.y)
    b.bin = {"screencapture": "/usr/sbin/screencapture", "sips": "/usr/bin/sips"}
    b.cg = _mock.MagicMock()
    b.cg.CGPreflightScreenCaptureAccess.return_value = access
    b.cg.CGDisplayCreateImageForRect.return_value = 99
    b.cg.CGImageGetWidth.return_value = native[0]
    b.cg.CGImageGetHeight.return_value = native[1]
    b.cf, b.imageio, b.ct = _mock.MagicMock(), _mock.MagicMock(), _mock.MagicMock()
    b.imageio.CGImageDestinationFinalize.return_value = True
    b.ct.string_at.return_value = b"JPEG"
    b._jpeg_uti = 1
    b.screen_state = lambda: screen or {"locked": False, "asleep": False}
    return b


MON = jd.Monitor(name="main", x=0, y=0, w=1280, h=800)


@_mac
def test_cg_image_uses_whole_monitor_or_the_region_in_monitor_points():
    b = _mac_backend()
    img, nw, nh = b._cg_image(MON, None)
    assert (img, nw, nh) == (99, 2560, 1600)
    assert b.cg.CGDisplayCreateImageForRect.call_args[0] == (7, (0, 0, 1280, 800))
    b._cg_image(MON, (100, 50, 300, 200))
    assert b.cg.CGDisplayCreateImageForRect.call_args[0] == (7, (100, 50, 300, 200))


@_mac
def test_cg_image_declines_without_permission_display_or_picture_and_refuses_locked():
    assert _mac_backend(access=False)._cg_image(MON, None) is None
    b = _mac_backend()
    b._dids = {}
    assert b._cg_image(MON, None) is None
    b = _mac_backend()
    b.cg.CGDisplayCreateImageForRect.return_value = None
    assert b._cg_image(MON, None) is None
    b = _mac_backend(screen={"locked": True, "asleep": False})
    with pytest.raises(jd.DeskError):
        b._cg_image(MON, None)


@_mac
def test_cg_thumbnail_scales_the_long_edge_to_thumb_edge_as_pgm():
    b = _mac_backend((2560, 1600))
    seen = {}

    def draw(img, tw, th, grey):
        seen.update(tw=tw, th=th, grey=grey)
        return 5, bytes(tw * th)
    b._draw = draw
    t = b._cg_thumbnail(MON, None)
    assert (seen["tw"], seen["th"], seen["grey"]) == (160, 100, True)
    assert t.startswith(b"P5\n160 100\n255\n") and len(t) == len(b"P5\n160 100\n255\n") + 16000
    b.cg.CGImageRelease.assert_called_with(99)
    b.cg.CGContextRelease.assert_called_with(5)


@_mac
def test_cg_screenshot_target_size_full_frame_and_zoom():
    b = _mac_backend((2560, 1600))
    seen = []
    b._draw = lambda img, tw, th, grey: (seen.append((tw, th)) or 5, None)
    assert b._cg_screenshot(MON, None) == b"JPEG"
    assert seen[-1] == (jd.LONG_EDGE, 800)                    # 2x native -> long edge 1280
    b = _mac_backend((600, 400))                              # region already at native size
    seen.clear()
    b._draw = lambda img, tw, th, grey: (seen.append((tw, th)) or 5, None)
    assert b._cg_screenshot(MON, (0, 0, 300, 200)) is not None
    want = max(jd.zoom_size(300, 200, 600 / 300))
    assert max(seen[-1]) == want and seen[-1][0] / seen[-1][1] == pytest.approx(1.5, rel=0.02)
    b.cg.CGImageRelease.assert_any_call(99)


@_mac
def test_cg_screenshot_none_when_bitmap_context_fails():
    b = _mac_backend()
    b._draw = lambda *a, **k: (None, None)
    assert b._cg_screenshot(MON, None) is None
    b.cg.CGImageRelease.assert_called_with(99)


def _fake_run(calls):
    def run(argv, **kw):
        calls.append(argv)
        if argv[0].endswith("screencapture"):
            Path(argv[-1]).write_bytes(b"jpegbytes")
        elif argv[0].endswith("sips"):
            out = argv[argv.index("--out") + 1] if "--out" in argv else argv[-1]
            Path(out).write_bytes(b"resized")
    return run


@_mac
def test_screenshot_falls_back_to_screencapture_and_sips_when_in_process_fails(monkeypatch):
    b = _mac_backend()
    b._cg_screenshot = lambda mon, rect: None
    calls = []
    monkeypatch.setattr(jd, "run_argv", _fake_run(calls))
    monkeypatch.setattr(jd, "image_size", lambda d: (2560, 1600))
    out = b.screenshot(jd.Monitor(name="main", x=10, y=20, w=1280, h=800), (5, 6, 100, 80))
    assert calls[0][:4] == ["/usr/sbin/screencapture", "-x", "-R", "15,26,100,80"]
    assert calls[1][:2] == ["/usr/bin/sips", "-Z"] and out == b"resized"


@_mac
def test_screenshot_falls_back_when_in_process_bytes_are_not_an_image(monkeypatch):
    b = _mac_backend()
    b._cg_screenshot = lambda mon, rect: b"garbage"
    calls = []
    monkeypatch.setattr(jd, "run_argv", _fake_run(calls))
    monkeypatch.setattr(jd, "image_size", lambda d: None if d == b"garbage" else (1280, 800))
    b.screenshot(MON)
    assert calls[0][0] == "/usr/sbin/screencapture"
    assert len(calls) == 1                                    # already at target: no sips


@_mac
def test_thumbnail_falls_back_to_bmp_and_a_locked_screen_is_refused(monkeypatch):
    b = _mac_backend()
    b._cg_thumbnail = lambda mon, rect: None
    calls = []
    monkeypatch.setattr(jd, "run_argv", _fake_run(calls))
    monkeypatch.setattr(jd, "image_size", lambda d: (2560, 1600))
    assert b.thumbnail(MON) == b"resized"
    sips = calls[1]
    assert sips[:2] == ["/usr/bin/sips", "-z"] and sips[2:4] == ["100", "160"]
    assert "bmp" in sips
    b = _mac_backend(screen={"locked": True, "asleep": False})
    b._cg_thumbnail = lambda mon, rect: None
    with pytest.raises(jd.DeskError):
        b.thumbnail(MON)
