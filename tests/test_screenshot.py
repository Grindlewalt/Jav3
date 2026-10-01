"""WP5 in-guest screenshot tool: arg clamps, URL/loopback rules, the
downscale/format rule, /proc/net/tcp proxy detection, the with_image path, the
url/app flows with chromium/Xvfb/the gateway mocked, and guest packaging."""
import asyncio
import importlib.util
import io
import json
import sys
import tarfile
import types

import pytest

from backend.agent import imageresult
from backend.config import settings

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location(
        "t_screenshot", settings.base_dir / "tools" / "screenshot" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


shot = _load()


def _png(w, h, noisy=False) -> bytes:
    if noisy:
        import os
        im = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    else:
        im = Image.new("RGB", (w, h), (30, 90, 200))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def test_clamp():
    assert shot.clamp(99_999, 5000, 5000) == (15_000, 1600, 1200)
    assert shot.clamp(-1, 10, 10) == (0, 200, 200)
    assert shot.clamp("x", None, "800") == (2000, 1280, 800)


@pytest.mark.parametrize("url,loop", [
    ("http://localhost:5173/", True), ("http://127.0.0.1:3000", True),
    ("http://[::1]:8000/x", True), ("http://app.localhost/", True),
    ("https://example.com", False), ("http://10.201.0.1:8443", False),
    ("http://localhost.evil.com/", False), ("http://127.0.0.1.nip.io/", False),
])
def test_loopback(url, loop):
    assert shot.is_loopback_url(shot.check_url(url)) is loop


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "javascript:alert(1)", "ftp://x",
                                 "--no-sandbox", "http://", "http://a b", "", None,
                                 "chrome://settings", "data:text/html,hi"])
def test_bad_urls(bad):
    with pytest.raises(ValueError):
        shot.check_url(bad)


def test_argv_remote_detection():
    assert shot.argv_mentions_remote(["firefox", "https://evil.example"])
    assert shot.argv_mentions_remote(["app", "--url=http://x.com"])
    assert not shot.argv_mentions_remote(["app", "--url=http://localhost:3000"])
    assert not shot.argv_mentions_remote(["xclock", "-digital"])


def test_encode_downscales_and_keeps_png():
    data, mime, (w, h) = shot.encode_image(_png(2560, 1440))
    assert mime == "image/png" and (w, h) == (1280, 720)
    assert imageresult.sniff(data) == "image/png"
    data, mime, size = shot.encode_image(_png(800, 600))
    assert size == (800, 600)


def test_encode_switches_to_jpeg_over_1mb():
    data, mime, (w, h) = shot.encode_image(_png(1200, 900, noisy=True))
    assert mime == "image/jpeg" and imageresult.sniff(data) == "image/jpeg"
    assert w == 1200
    data, mime, (w, h) = shot.encode_image(_png(1000, 9000))
    assert h <= shot.MAX_H


def test_proxy_conns_parse(tmp_path):
    # 10.201.0.1:8443 == 0100C90A:20FB ; local 10.201.0.2:40000 == 0200C90A:9C40
    tcp = tmp_path / "tcp"
    tcp.write_text(
        "  sl  local_address rem_address   st\n"
        "   0: 0200C90A:9C40 0100C90A:20FB 06 0 0\n"      # TIME_WAIT to the proxy
        "   1: 0100007F:1F90 0100007F:9C41 01 0 0\n"      # loopback, ignored
        "   2: 0200C90A:9C42 0101A8C0:0050 01 0 0\n")     # elsewhere, ignored
    tcp6 = tmp_path / "tcp6"
    tcp6.write_text("  sl  local rem st\n"
                    "   0: 0000000000000000FFFF00000200C90A:9C43 "
                    "0000000000000000FFFF00000100C90A:20FB 01 0\n")
    got = shot.proxy_conns("10.201.0.1", 8443, paths=(str(tcp), str(tcp6)))
    assert got == {("10.201.0.2", 40000), ("10.201.0.2", 40003)}


def test_chromium_argv_uses_proxy_and_url_last():
    argv = shot.chromium_argv("/usr/bin/chromium", "https://example.com", "/tmp/o.png",
                              1280, 800, 2000, "http://10.201.10.1:8443", "/tmp/p", root=True)
    assert "--headless=new" in argv and "--proxy-server=http://10.201.10.1:8443" in argv
    assert argv[-1] == "https://example.com" and "--no-sandbox" in argv
    assert "--window-size=1280,800" in argv


def test_host_side_refuses():
    assert asyncio.run(shot.run(mode="url", url="http://localhost")).startswith("error")


@pytest.fixture
def guest(monkeypatch, tmp_path):
    """Pretend to be the guest: in_guest settings, turnctx, a shot dir."""
    monkeypatch.setattr(settings, "in_guest", True, raising=False)
    monkeypatch.setattr(settings, "projects_dir", tmp_path / "projects")
    monkeypatch.setattr(shot, "SHOT_DIR", str(tmp_path / "shots"))
    tc = types.ModuleType("backend.turnctx")
    import contextvars
    tc.op_id = contextvars.ContextVar("op", default="op-1")
    tc.op_token = contextvars.ContextVar("tok", default="t")
    tc.gateway_port = contextvars.ContextVar("gp", default=5555)
    monkeypatch.setitem(sys.modules, "backend.turnctx", tc)
    import backend
    monkeypatch.setattr(backend, "turnctx", tc, raising=False)
    notes = []

    async def taint(source):
        notes.append(source)
        return True
    monkeypatch.setattr(shot, "taint_note", taint)
    return notes


def _fake_chromium(monkeypatch, png: bytes, calls: list):
    monkeypatch.setattr(shot.shutil, "which",
                        lambda n: "/usr/bin/" + n if n in ("chromium", "Xvfb", "scrot") else None)

    async def fake_exec(argv, *, timeout, env=None, cwd=None, watch_oom=False):
        calls.append(argv)
        out = next((a.split("=", 1)[1] for a in argv if a.startswith("--screenshot=")), None)
        if out is None and argv[0] == "scrot":
            out = argv[-1]
        with open(out, "wb") as f:
            f.write(png)
        return shot.Ran(0)
    monkeypatch.setattr(shot, "_exec", fake_exec)


def test_url_mode_local_no_taint(guest, monkeypatch):
    calls = []
    _fake_chromium(monkeypatch, _png(1600, 1000), calls)
    out = asyncio.run(shot.run(mode="url", url="http://localhost:5173/", width=1600,
                               height=1000))
    text, img = imageresult.split(out)
    assert img is not None and img.path.endswith(".png")
    assert "1280x800" in text and guest == []
    data = open(img.path, "rb").read()
    assert imageresult.sniff(data) == "image/png"
    assert Image.open(io.BytesIO(data)).size == (1280, 800)
    assert any(a.startswith("--proxy-server=") for a in calls[0])


def test_url_mode_remote_taints_first(guest, monkeypatch):
    calls = []
    _fake_chromium(monkeypatch, _png(800, 600), calls)
    out = asyncio.run(shot.run(mode="url", url="https://example.com", full_page=True))
    assert guest == ["screenshot:url"]
    assert "tainted" in out and imageresult.split(out)[1] is not None
    assert "--window-size=1280,4000" in calls[0]


def test_url_mode_withholds_when_taint_fails(guest, monkeypatch):
    calls = []
    _fake_chromium(monkeypatch, _png(800, 600), calls)

    async def fail(source):
        return False
    monkeypatch.setattr(shot, "taint_note", fail)
    out = asyncio.run(shot.run(mode="url", url="https://example.com"))
    assert out.startswith("error") and calls == []


def test_needs_desktop_image(guest, monkeypatch):
    monkeypatch.setattr(shot.shutil, "which", lambda n: None)
    assert asyncio.run(shot.run(mode="url", url="http://localhost")) == shot.NEEDS_DESKTOP
    assert asyncio.run(shot.run(mode="app", command=["xclock"])) == shot.NEEDS_DESKTOP


def test_app_mode_taints_on_proxy_traffic(guest, monkeypatch, tmp_path):
    calls = []
    _fake_chromium(monkeypatch, _png(1024, 768), calls)

    class FakeX:
        async def ensure(self, w, h):
            calls.append(("xvfb", w, h))

        def arm_idle(self):
            calls.append("idle")
    monkeypatch.setattr(shot, "xvfb", FakeX())
    tk = types.ModuleType("toolctx")

    async def slug():
        return "demo"
    tk.active_slug = slug
    import backend.agent.tools as bat
    monkeypatch.setattr(bat, "toolctx", tk, raising=False)
    monkeypatch.setitem(sys.modules, "backend.agent.tools.toolctx", tk)
    snaps = iter([set(), {("10.201.0.2", 41000)}])
    monkeypatch.setattr(shot, "proxy_conns", lambda h, p: next(snaps))

    class Proc:
        pid = 999999

        async def wait(self):
            return 0

    async def fake_spawn(*argv, **kw):
        calls.append(("spawn", argv, kw["env"]["DISPLAY"], kw["cwd"]))
        return Proc()
    monkeypatch.setattr(shot.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(shot.os.path, "exists",
                        lambda p, _e=shot.os.path.exists: True if p == "xclock" else _e(p))
    out = asyncio.run(shot.run(mode="app", command=["xclock", "-digital"], wait_ms=0))
    assert guest == ["screenshot:app"], out
    spawn = next(c for c in calls if isinstance(c, tuple) and c[0] == "spawn")
    assert spawn[1] == ("xclock", "-digital") and spawn[2] == ":99"
    assert spawn[3].endswith("projects/demo")
    assert "idle" in calls and imageresult.split(out)[1] is not None


def test_app_mode_bad_command(guest, monkeypatch):
    monkeypatch.setattr(shot.shutil, "which", lambda n: "/usr/bin/" + n)
    for bad in ("xclock -digital", [], [""], ["a\x00b"], [1, 2]):
        assert asyncio.run(shot.run(mode="app", command=bad)).startswith("error")


def test_guest_package_includes_screenshot_only_with_boxes(monkeypatch):
    from backend.vm import guest_pkg
    assert "screenshot" in guest_pkg.IN_GUEST_TOOLS

    def names():
        tar = tarfile.open(fileobj=io.BytesIO(guest_pkg.build_package_tar()))
        return set(tar.getnames())
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    assert "tools/screenshot/handler.py" in names()
    monkeypatch.setattr(settings, "vm_boxes_enabled", False)
    assert "tools/screenshot/handler.py" not in names()
    src = (settings.base_dir / "guest" / "backend" / "agent" / "tools" / "registry.py").read_text()
    assert "from .inguest import" in src  # the guest dispatches it locally, by the shared list


def test_tool_specs_gated_on_boxes(monkeypatch):
    from backend.agent.tools import registry
    monkeypatch.setattr(settings, "vm_boxes_enabled", False)
    assert "screenshot" not in {s["function"]["name"] for s in registry.openai_tool_specs()}
    assert "package_request" not in {s["function"]["name"] for s in registry.openai_tool_specs()}
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    names = {s["function"]["name"] for s in registry.openai_tool_specs()}
    assert {"screenshot", "package_request"} <= names


def test_taint_note_wire(monkeypatch):
    """The real taint_note sends the contract's op with the turn's token."""
    mod = _load()
    import contextvars
    tc = types.ModuleType("backend.turnctx")
    tc.op_id = contextvars.ContextVar("op", default="op-9")
    tc.op_token = contextvars.ContextVar("tok", default="tok-9")
    tc.gateway_port = contextvars.ContextVar("gp", default=5555)
    import backend
    monkeypatch.setitem(sys.modules, "backend.turnctx", tc)
    monkeypatch.setattr(backend, "turnctx", tc, raising=False)
    sent = []

    class S:
        def settimeout(self, t):
            pass

        def sendall(self, b):
            sent.append(json.loads(b))

        def makefile(self, mode):
            return io.BytesIO(b'{"type":"taint_noted","tainted":true,"newly":true}\n')

        def close(self):
            pass
    bi = types.ModuleType("backend.boxinfo")
    bi.gateway_connect = lambda: S()
    monkeypatch.setitem(sys.modules, "backend.boxinfo", bi)
    monkeypatch.setattr(backend, "boxinfo", bi, raising=False)
    assert asyncio.run(mod.taint_note("screenshot:url")) is True
    assert sent == [{"op": "taint_note", "op_id": "op-9", "op_token": "tok-9",
                     "source": "screenshot:url"}]


# --- C1: why a screenshot failed ------------------------------------------------------
# 2026-10-01, benchmark-game: a WebGL game page outgrew the 975 MB work cgroup, the
# kernel killed chromium's renderer and the rest hung until the tool's 60 s timeout
# (seven times in one turn). The result said only "chromium failed (exit -9)", so the
# model retried it unchanged. Now the result names the cause and what to change.

from backend import memguard  # noqa: E402


def _counter(monkeypatch, seq):
    """memguard.oom_kills reads from seq, then repeats its last value."""
    it = iter(seq)
    last = [seq[-1]]

    def kills(path=None):
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]
    monkeypatch.setattr(memguard, "oom_kills", kills)
    monkeypatch.setattr(memguard, "setup", lambda: None)


def test_exec_stops_a_hung_chromium_after_the_kernel_kill(monkeypatch):
    _counter(monkeypatch, [0, 0, 1])
    monkeypatch.setattr(shot, "OOM_POLL_S", 0.05)
    monkeypatch.setattr(shot, "OOM_GRACE_S", 0.1)
    ran = asyncio.run(shot._exec(["sleep", "30"], timeout=20, watch_oom=True))
    assert ran.rc == -9 and not ran.timed_out and ran.oom_kills == 1
    assert ran.secs < 5                      # not the 20 s timeout


def test_exec_plain_timeout_and_clean_exit(monkeypatch):
    _counter(monkeypatch, [0])
    ran = asyncio.run(shot._exec(["sleep", "30"], timeout=0.3, watch_oom=True))
    assert ran.rc == -9 and ran.timed_out and ran.oom_kills == 0
    ran = asyncio.run(shot._exec(["sleep", "30"], timeout=0.3))      # not watching: same
    assert ran.rc == -9 and ran.timed_out
    ran = asyncio.run(shot._exec(["true"], timeout=5, watch_oom=True))
    assert ran == shot.Ran(0, False, 0, ran.secs)


def test_exec_lets_chromium_finish_inside_the_grace(monkeypatch):
    _counter(monkeypatch, [0, 1])
    monkeypatch.setattr(shot, "OOM_POLL_S", 0.05)
    monkeypatch.setattr(shot, "OOM_GRACE_S", 5)
    ran = asyncio.run(shot._exec(["sleep", "0.3"], timeout=20, watch_oom=True))
    assert ran.rc == 0 and ran.oom_kills == 1


def test_failure_text_names_the_cause_and_what_to_change():
    url = "http://127.0.0.1:5173/scripts/shot.html?rd=4"
    oom = shot.failure_text("oom", url, shot.Ran(-9, False, 1, 9.4), 975)
    assert oom.startswith("error: chromium was killed for using more than the 975 MB")
    assert "about 575 MB" in oom and "raise this project's RAM" in oom
    assert "smaller width and height" in oom and "exit -9" not in oom
    assert "same failure" not in oom
    to = shot.failure_text("timeout", url, shot.Ran(-9, True, 0, 60.2), 975)
    assert "timed out after 60 s" in to and "wait_ms + 45 s" in to
    assert "killed nothing for memory" in to and "shorter wait_ms" in to
    kd = shot.failure_text("killed", url, shot.Ran(-9, False, 0, 3.0), 0)
    assert "SIGKILL" in kd and "not by this tool's timeout" in kd
    ex = shot.failure_text("exit", url, shot.Ran(1, False, 0, 2.0), 975)
    assert "chromium failed (exit 1)" in ex and "nothing is listening" in ex
    # no cap known (a box without a memory controller): no invented number
    assert "MB" not in shot.failure_text("oom", url, shot.Ran(-9, False, 1, 9), 0).split("chromium was killed")[1].split("The turn")[0]
    assert shot.failure_kind(shot.Ran(-9, True, 2, 60)) == "oom"      # the kill is the cause
    assert shot.failure_kind(shot.Ran(-9, True, 0, 60)) == "timeout"
    assert shot.failure_kind(shot.Ran(-9, False, 0, 5)) == "killed"
    assert shot.failure_kind(shot.Ran(2, False, 0, 5)) == "exit"


@pytest.fixture
def _fresh_fails():
    shot._fails.update(op=None, pages={})
    yield
    shot._fails.update(op=None, pages={})


def _failing_chromium(monkeypatch, ran):
    monkeypatch.setattr(shot.shutil, "which", lambda n: "/usr/bin/" + n)
    calls = []

    async def fake_exec(argv, *, timeout, env=None, cwd=None, watch_oom=False):
        calls.append((timeout, watch_oom))
        return ran
    monkeypatch.setattr(shot, "_exec", fake_exec)
    monkeypatch.setattr(memguard, "limit_now_mb", lambda: 975)
    return calls


def test_url_mode_says_why_and_flags_the_repeat(guest, monkeypatch, _fresh_fails):
    calls = _failing_chromium(monkeypatch, shot.Ran(-9, False, 1, 9.0))
    page = "http://127.0.0.1:5173/scripts/shot.html?seed=7&rd="
    first = asyncio.run(shot.run(mode="url", url=page + "4", wait_ms=14000, width=960,
                                 height=600))
    assert first.startswith("error: chromium was killed for using more than the 975 MB")
    assert "same failure" not in first
    assert calls == [(59.0, True)]           # wait_ms/1000 + 45, and watching the kernel
    again = asyncio.run(shot.run(mode="url", url=page + "4", wait_ms=14000, width=960,
                                 height=600))
    assert again.startswith("error: same failure as the previous call (2 in a row this "
                            "turn), don't retry unchanged. ")
    assert "Changing only the page's parameters" not in again      # the call was identical
    changed = asyncio.run(shot.run(mode="url", url=page + "3", wait_ms=14000, width=960,
                                   height=600))
    assert "(3 in a row this turn)" in changed
    assert "Changing only the page's parameters has not helped." in changed
    other_page = asyncio.run(shot.run(mode="url", url="http://127.0.0.1:5173/probe.html"))
    assert "same failure" not in other_page


def test_repeat_count_is_per_turn_and_a_success_clears_it(guest, monkeypatch, _fresh_fails):
    import backend.turnctx as tc
    calls = _failing_chromium(monkeypatch, shot.Ran(-9, True, 0, 60.0))
    url = "http://127.0.0.1:5173/scripts/shot.html"
    a = asyncio.run(shot.run(mode="url", url=url))
    assert "timed out after 60 s" in a and "same failure" not in a
    assert "same failure" in asyncio.run(shot.run(mode="url", url=url))
    tc.op_id.set("op-2")                       # the next turn starts clean
    assert "same failure" not in asyncio.run(shot.run(mode="url", url=url))
    assert "same failure" in asyncio.run(shot.run(mode="url", url=url))
    # a different kind of failure is not "the same failure"
    _failing_chromium(monkeypatch, shot.Ran(-9, False, 1, 9.0))
    assert "same failure" not in asyncio.run(shot.run(mode="url", url=url))
    # a capture that works clears the page's record
    _fake_chromium(monkeypatch, _png(300, 200), [])
    assert imageresult.split(asyncio.run(shot.run(mode="url", url=url)))[1] is not None
    _failing_chromium(monkeypatch, shot.Ran(-9, True, 0, 60.0))
    assert "same failure" not in asyncio.run(shot.run(mode="url", url=url))
    assert calls


def test_url_mode_keeps_an_image_that_chromium_wrote_before_it_was_stopped(
        guest, monkeypatch, _fresh_fails):
    calls = []
    _fake_chromium(monkeypatch, _png(400, 300), calls)

    async def exec_(argv, *, timeout, env=None, cwd=None, watch_oom=False):
        out = next(a.split("=", 1)[1] for a in argv if a.startswith("--screenshot="))
        with open(out, "wb") as f:
            f.write(_png(400, 300))
        return shot.Ran(-9, False, 1, 12.0)         # a kill showed, the file is there
    monkeypatch.setattr(shot, "_exec", exec_)
    text, img = imageresult.split(asyncio.run(shot.run(mode="url", url="http://localhost:1/")))
    assert img is not None and "may be incomplete" in text


def test_app_mode_names_a_memory_kill(guest, monkeypatch, tmp_path):
    monkeypatch.setattr(shot.shutil, "which",
                        lambda n: "/usr/bin/" + n if n in ("Xvfb", "xclock") else None)

    class FakeX:
        async def ensure(self, w, h):
            pass

        def arm_idle(self):
            pass
    monkeypatch.setattr(shot, "xvfb", FakeX())
    tk = types.ModuleType("toolctx")

    async def slug():
        return "demo"
    tk.active_slug = slug
    import backend.agent.tools as bat
    monkeypatch.setattr(bat, "toolctx", tk, raising=False)
    monkeypatch.setitem(sys.modules, "backend.agent.tools.toolctx", tk)
    monkeypatch.setattr(shot, "proxy_conns", lambda h, p: set())
    _counter(monkeypatch, [0, 1])
    monkeypatch.setattr(memguard, "limit_now_mb", lambda: 975)

    class Proc:
        pid = 999999

        async def wait(self):
            return 0

    async def fake_spawn(*argv, **kw):
        return Proc()
    monkeypatch.setattr(shot.asyncio, "create_subprocess_exec", fake_spawn)
    out = asyncio.run(shot.run(mode="app", command=["xclock"], wait_ms=0))
    assert out.startswith("error: the app or its display was killed for using more than the "
                          "975 MB")
    assert "raise this project's RAM" in out
