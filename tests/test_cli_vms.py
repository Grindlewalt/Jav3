"""/vms in the terminal client: one-line box rows (idle timers, what a box is
doing), the history pane, the nuke / restart / destroy / clean keys, the
image build log, and the live refresh that keeps the cursor on its box."""
import copy
import importlib.machinery
import importlib.util
import json
import time
from pathlib import Path

import httpx
import pytest

CLI = Path(__file__).resolve().parent.parent / "clients" / "jav3cli" / "jav3"


def _load():
    loader = importlib.machinery.SourceFileLoader("jav3cli_vms", str(CLI))
    spec = importlib.util.spec_from_loader("jav3cli_vms", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


jav3 = _load()


def _closure(fn, name):
    for n, c in zip(fn.__code__.co_freevars, fn.__closure__ or ()):
        if n == name:
            return c.cell_contents
    raise KeyError(name)


@pytest.fixture(scope="module")
def vms():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", "session:sess")
    return _closure(type(app).c_vms, "VmsScreen")


SHARED = {"id": "shared", "kind": "shared", "project": None, "projects": [], "cid": 3,
          "runtime": "kvm", "state": "running", "activity": "idle", "mem_mb": 768,
          "ram_cost_mb": 912, "image": {"variant": "main", "version": 4},
          "uptime_s": 312, "rss_bytes": 500_000_000, "cpu_pct": 4, "inflight": 0,
          "idle_s": 240, "stop_action": "scrub", "stop_after_s": 900, "stops_in_s": 660,
          "started_at": time.time() - 312, "now": [], "last_event": None,
          "last_error": None, "disk": {}, "net": {"tap": "jvtap0"}}
ALPHA = {"id": "p-alpha", "kind": "project", "project": "alpha",
         "projects": ["alpha", "beta"], "cid": 10, "runtime": "docker", "state": "running",
         "activity": "idle", "mem_mb": 512, "ram_cost_mb": 512,
         "image": {"variant": "main", "version": None}, "uptime_s": 900,
         "rss_bytes": 40_000_000, "cpu_pct": 1, "inflight": 0, "idle_s": 240,
         "stop_action": "stop", "stop_after_s": 600, "stops_in_s": 360,
         "started_at": time.time() - 900, "now": [], "last_event": None,
         "last_error": None, "disk": {}, "net": {"tap": "jvbr10"}}


def _row(vms, raw):
    return vms.row_markup({"type": "box", "key": f"X{raw['id']}", "raw": raw})


# --- pure: rows -----------------------------------------------------------------

def test_idle_timer_words_follow_the_backlog_spec(vms):
    assert "· idle 4m (stops at 10m)" in _row(vms, ALPHA)
    assert "⌂ alpha +beta" in _row(vms, ALPHA)
    assert "less isolated" in _row(vms, ALPHA)
    assert "· scrub after 11m" in _row(vms, SHARED)
    off = {**SHARED, "stop_action": None, "stop_after_s": None, "stops_in_s": None}
    assert "scrub" not in _row(vms, off)
    busy = {**ALPHA, "inflight": 1, "idle_s": None, "activity": "busy",
            "now": [{"op_id": "chat:42", "conversation_id": 42, "title": "fix the build",
                     "project": "alpha",
                     "tool": {"name": "shell", "detail": "npm test", "since": time.time()}}]}
    r = _row(vms, busy)
    assert "idle" not in r and "#42" in r and "fix the build" in r
    assert "shell" in r and "npm test" in r


def test_stopped_row_says_what_happened_last_and_errors_in_red(vms):
    st = {**ALPHA, "state": "stopped", "activity": "stopped", "idle_s": None,
          "last_event": {"event": "idle_stopped", "created_at": "2026-09-28 14:03:00"}}
    r = _row(vms, st)
    assert "stopped" in r and "idle-stopped" in r and "stops at" not in r
    bad = {**st, "activity": "failed", "last_error": "docker run failed: no image"}
    r = _row(vms, bad)
    assert "[$error]failed[/]" in r and "✗ docker run failed" in r


def test_leftover_rows_follow_the_boxes(vms):
    es = vms.box_entries([SHARED, ALPHA], {"items": [
        {"id": "qemu:4001", "type": "qemu", "name": "qemu pid 4001", "why": "left over",
         "cleanable": True},
        {"id": "tap:jvtap12", "type": "tap", "name": "jvtap12", "why": "no box",
         "cleanable": False}]})
    assert [e["type"] for e in es] == ["box", "box", "group", "leftover", "leftover"]
    assert "▲" in vms.row_markup(es[3]) and "△" in vms.row_markup(es[4])
    assert [e["type"] for e in vms.box_entries([SHARED], {"items": []})] == ["box"]


def test_history_line_and_mins(vms):
    line = _closure(vms._detail_box, "_box_event_line")(
        {"event": "idle_stopped", "reason": "idle 11m (stops at 10m)", "actor": "reaper",
         "created_at": "2026-09-28 14:03:00"})
    assert "IDLE-STOPPED" in line and "by reaper" in line and "idle 11m" in line
    assert "[$text-muted]" in line
    crash = _closure(vms._detail_box, "_box_event_line")({"event": "crashed"})
    assert "[$error b]CRASHED" in crash
    mins = _closure(_closure(vms.row_markup, "_ROWS")["box"], "_box_timer")
    assert mins({**ALPHA, "idle_s": 30}).strip() == "[$text-muted]· idle 30s (stops at 10m)[/]"


# --- the screen, against a fake server ---------------------------------------------

def _server(seen):
    state = {
        "boxes": [copy.deepcopy(SHARED), copy.deepcopy(ALPHA)],
        "leftovers": {"items": [
            {"id": "container:jav3-p-ghost", "type": "container", "name": "jav3-p-ghost",
             "why": "container exited: no box p-ghost exists", "cleanable": True},
            {"id": "box_dir:p-ghost", "type": "box_dir", "name": "boxes/p-ghost",
             "why": "box directory with no box registered", "cleanable": True,
             "bytes": 20_000_000}], "cleanable": 2, "docker": "on"},
        "events": [{"id": 2, "event": "started", "actor": "turn chat:42", "reason": None,
                    "created_at": "2026-09-28 14:00:00"},
                   {"id": 1, "event": "destroyed", "actor": "operator grant",
                    "reason": "operator destroy", "created_at": "2026-09-28 13:00:00"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        if "jarvis_token=sess" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"detail": "not authenticated"})
        seen.append((method, path, dict(request.url.params), body))
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "operator"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={"default": "deepseek/deepseek-flash",
                                             "models": [], "projects": [], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        if path == "/api/agents/notices/stream":
            return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})
        if path == "/api/vm/boxes":
            return httpx.Response(200, json={
                "enabled": True, "boxes": state["boxes"],
                "budget": {"ram_mb_used": 1424, "ram_mb_cap": 2400, "boxes": 2,
                           "boxes_cap": 4, "project_boxes": 1, "project_boxes_cap": 3},
                "runtimes": {"kvm": {"available": True}, "docker": {"available": True}},
                "idle": {"project_stop_s": 600, "shared_scrub_s": 900,
                         "reaper_interval_s": 30}})
        if path == "/api/vm/leftovers":
            return httpx.Response(200, json=state["leftovers"])
        if path == "/api/vm/leftovers/clean" and method == "POST":
            removed = [i["id"] for i in state["leftovers"]["items"]]
            state["leftovers"] = {"items": [], "cleanable": 0}
            return httpx.Response(200, json={"removed": removed, "failed": [],
                                             "skipped": [], "left": state["leftovers"]})
        if path.startswith("/api/vm/boxes/") and path.endswith("/events"):
            return httpx.Response(200, json={"box_id": path.split("/")[4],
                                             "events": state["events"]})
        if path == "/api/vm/nuke" and method == "POST":
            return httpx.Response(200, json={"image_version": "v4", "running": True})
        if path.startswith("/api/vm/boxes/") and method == "POST":
            return httpx.Response(200, json=state["boxes"][1])
        if path == "/api/vm/images":
            return httpx.Response(200, json={"build": {"running": False}, "variants": [
                {"name": "dev", "from": "main", "min_mem_mb": 768, "used_by": ["site"],
                 "last_build": {"version": 3, "ok": False, "error": "disk full",
                                "finished_at": "2026-09-28 10:00:00", "log_tail": ["x"]},
                 "versions": [{"version": 2, "status": "built", "active": True}]}]})
        if path == "/api/vm/images/dev/log":
            v = request.url.params.get("version")
            return httpx.Response(200, json={
                "variant": "dev", "version": int(v or 3), "running": False,
                "ok": v == "2", "error": None if v == "2" else "disk full",
                "lines": ["Setting up golang [1]", "[b]not markup[/b]"]})
        return httpx.Response(404, json={"detail": "nope"})
    return httpx.MockTransport(handler), state


async def _until(pilot, cond, n=80):
    for _ in range(n):
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


def _rows(scr):
    return [str(r.render()) for r in scr.query("SecRow")]


def _text(w):
    return str(w.render())


def _posts(seen):
    return [(m, p, b) for m, p, _, b in seen if m != "GET"]


async def test_tui_vms_rows_keys_and_live_refresh(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    seen: list = []
    tr, state = _server(seen)
    app = jav3.build_tui("http://h:1", "session:sess", transport=tr)
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/vms")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "VmsScreen")
        scr = app.screen
        assert await _until(pilot, lambda: scr.loaded["boxes"] and len(_rows(scr)) == 5)
        rows = _rows(scr)
        assert "scrub after 11m" in rows[0] and "idle 4m (stops at 10m)" in rows[1]
        assert "leftovers" in rows[2] and "jav3-p-ghost" in rows[3]
        sub = _text(scr.query_one("#sec-sub"))
        assert "project boxes stop after 10m idle" in sub and "2 leftovers" in sub
        foot = _text(scr.query_one("#sec-foot"))
        assert "x nuke shared" in foot and "c clean leftovers" in foot and "refresh" not in foot

        # x on the shared box: the BACKLOG's Confirm, then POST /api/vm/nuke
        scr.select_key("Xshared")
        await pilot.press("x")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Confirm")
        assert app.screen.question == "Nuke the shared box?"
        assert app.screen.detail == ("Its overlay disk is discarded and it reboots fresh "
                                     "from the golden image.")
        await pilot.press("y")
        assert await _until(pilot, lambda: ("POST", "/api/vm/nuke", {"confirm": True})
                            in _posts(seen))
        assert await _until(pilot, lambda: "nuked the shared box — fresh from main v4"
                            in _text(scr.query_one("#sec-sub")))

        # x on a project box does nothing but say so
        scr.select_key("Xp-alpha")
        await pilot.press("x")
        await pilot.pause(0.2)
        assert app.screen is scr and "x nukes the shared box" in _text(scr.query_one("#sec-sub"))

        # r restarts after a Confirm
        await pilot.press("r")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Confirm")
        assert app.screen.question == "Restart box p-alpha?" and "container" in app.screen.detail
        await pilot.press("y")
        assert await _until(pilot, lambda: ("POST", "/api/vm/boxes/p-alpha/restart", None)
                            in _posts(seen))
        assert await _until(pilot, lambda: app.screen is scr)

        # enter: the history in the detail pane; enter again hides it
        scr.select_key("Xp-alpha")
        await pilot.press("enter")
        assert await _until(pilot, lambda: "DESTROYED" in _text(scr.query_one("#sec-detail")))
        d = _text(scr.query_one("#sec-detail"))
        assert "STARTED" in d and "by turn chat:42" in d and "hide history" in d
        await pilot.press("enter")
        assert await _until(pilot, lambda: "DESTROYED" not in _text(scr.query_one("#sec-detail")))

        # live refresh: rows update in place, the cursor stays on p-alpha
        before = list(scr.query("SecRow"))
        state["boxes"][1].update(idle_s=300, stops_in_s=300)
        await scr.load("boxes")
        assert await _until(pilot, lambda: "idle 5m (stops at 10m)" in _rows(scr)[1])
        assert list(scr.query("SecRow")) == before           # the same widgets
        assert scr.sel["boxes"] == "Xp-alpha"

        # c cleans the leftovers after a Confirm naming them
        await pilot.press("c")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "Confirm")
        assert app.screen.question == "Clean 2 leftovers?"
        assert "jav3-p-ghost" in app.screen.detail and "never removed" in app.screen.detail
        await pilot.press("y")
        assert await _until(pilot, lambda: ("POST", "/api/vm/leftovers/clean",
                                            {"confirm": True}) in _posts(seen))
        assert await _until(pilot, lambda: len(_rows(scr)) == 2)
        assert "cleaned 2 leftovers" in _text(scr.query_one("#sec-sub"))

        # images: l opens the variant's whole build log, as text
        await pilot.press("2")
        assert await _until(pilot, lambda: scr.loaded["images"] and len(_rows(scr)) == 2)
        assert "failed" in _text(scr.query_one("#sec-detail"))      # last build
        await pilot.press("l")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "View")
        assert "[b]not markup[/b]" in app.screen.markup.replace("\\[", "[")
        assert "disk full" in app.screen.markup and "untrusted" in app.screen.markup
        await pilot.press("escape")
        assert await _until(pilot, lambda: app.screen is scr)
        await pilot.press("down", "l")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "View")
        assert any(p == "/api/vm/images/dev/log" and q == {"version": "2"}
                   for _, p, q, _ in seen)
        await pilot.press("escape")
        # r on images still refreshes
        n = len(seen)
        await pilot.press("r")
        assert await _until(pilot, lambda: any(p == "/api/vm/images" for _, p, _, _ in seen[n:]))
