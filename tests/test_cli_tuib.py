"""Second terminal-client hunt (TUIB-*): /login with a bad address, Enter on every
/security row, times, selected-row colours, Confirm keys, the picker's first letter,
scrolling the detail pane, 80x24 layouts, logged-out and server-down wording, and the
small ones. The client file is loaded from tests/cli_fake.py (JAV3_CLIENT points a run
at another copy, to watch a test fail on the base commit)."""
import io
import json
from pathlib import Path

import httpx
import pytest

from cli_fake import load_client, pin_zone, wait_for

jav3 = load_client("jav3cli_tuib")

SESSION = "session:sess"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """A throwaway config dir (the client saves tui.json, credentials) and UTC unless
    a test picks another zone."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    yield from pin_zone(monkeypatch)


def _zone(monkeypatch, name: str) -> None:
    import time
    monkeypatch.setenv("TZ", name)
    time.tzset()


def _srv(routes=None, seen=None):
    """A logged-in operator's server: `routes` maps a path to a JSON body (or a
    function of the request) and every other /api/... read is an empty list."""
    routes = routes or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if seen is not None:
            seen.append((method, path, dict(request.url.params),
                         request.content.decode() if request.content else ""))
        if "jarvis_token=sess" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"detail": "not authenticated"})
        hit = routes.get((method, path)) or routes.get(path)
        if hit is not None:
            body = hit(request) if callable(hit) else hit
            if isinstance(body, httpx.Response):
                return body
            return httpx.Response(200, json=body)
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "operator"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={"default": "deepseek/deepseek-flash",
                                             "models": [], "projects": [], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        if path in ("/api/agents/notices/stream", "/api/events"):
            return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})
        if path.startswith("/api/"):
            return httpx.Response(200, json={
                "projects": [], "pending": [], "events": [], "services": [], "packages": [],
                "secrets": [], "profiles": [], "boxes": [], "rows": [], "rules": []})
        return httpx.Response(404, json={"detail": "nope"})
    return httpx.MockTransport(handler)


async def _until(pilot, cond, tries=80):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


def _top(app) -> str:
    return type(app.screen).__name__


def _rows(scr):
    return [str(r.render()) for r in scr.query("SecRow")]


async def _security(pilot, app, tab="queue", cmd="/security"):
    await pilot.pause(0.3)
    app.dispatch(f"{cmd} {tab}".strip() if tab else cmd)
    assert await _until(pilot, lambda: _top(app) in ("SecurityScreen", "VmsScreen"))
    scr = app.screen
    assert await _until(pilot, lambda: scr.loaded.get(tab, True))
    await pilot.pause(0.15)
    return scr


# --- TUIB-01: a malformed server address is a note, never a crash -------------------------

@pytest.mark.parametrize("address", ["10.0.0.999:8000", "localhost:abc", "http://a b",
                                     "http://[::1"])
def test_base_url_rejects_a_malformed_address(address):
    with pytest.raises(jav3.CliError) as e:
        jav3.base_url(address)
    assert "not a valid server address" in str(e.value)


def test_base_url_still_takes_the_usual_forms():
    assert jav3.base_url("10.0.0.82:8000") == "http://10.0.0.82:8000"
    assert jav3.base_url(" http://box.local:8000/ ") == "http://box.local:8000"
    assert jav3.base_url("https://jarvis.example.com") == "https://jarvis.example.com"
    assert jav3.base_url("[::1]:8000") == "http://[::1]:8000"


def test_login_with_a_bad_address_is_a_cli_error():
    with pytest.raises(jav3.CliError):
        jav3.login_with_password("10.0.0.999:8000", "bob", "x")


async def test_slash_login_with_a_bad_address_leaves_the_app_running():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/login password")
        assert await _until(pilot, lambda: _top(app) == "Ask")
        app.screen.query_one("#answer").value = "10.0.0.999:8000"
        await pilot.press("enter")
        assert await _until(pilot, lambda: _top(app) == "Ask")      # username
        app.screen.query_one("#answer").value = "bob"
        await pilot.press("enter")
        assert await _until(pilot, lambda: _top(app) == "Ask")      # password
        app.screen.query_one("#answer").value = "x"
        await pilot.press("enter")
        assert await _until(pilot, lambda: any(
            "not a valid server address" in str(getattr(w, "render", lambda: "")())
            for w in app.query("Static")) or _top(app) != "Ask")
        await pilot.pause(0.3)
        assert app.is_running and _top(app) != "Ask"


async def test_an_unexpected_error_in_a_command_is_a_note_not_a_crash():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)

        async def boom(arg):
            raise RuntimeError("kaboom")
        app.commands["theme"].fn = boom
        notes: list = []
        real = app.note

        async def note(text, kind="info"):
            notes.append((text, kind))
            await real(text, kind)
        app.note = note
        app.dispatch("/theme")
        assert await wait_for(lambda: notes)
        assert app.is_running
        assert "kaboom" in notes[-1][0] and notes[-1][1] == "error"


# --- TUIB-02: Enter on a row never crashes ---------------------------------------------------

PROFILE = {"id": 1, "name": "Default", "hosts": ["pypi.org"], "allow_hosts": ["pypi.org"],
           "deny_hosts": [], "builtin": True}


async def test_enter_on_a_profile_row_opens_its_details():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION,
                         transport=_srv({"/api/profiles": {"profiles": [PROFILE]}}))
    async with app.run_test(size=(100, 30)) as pilot:
        scr = await _security(pilot, app, "profiles")
        assert await _until(pilot, lambda: len(_rows(scr)) >= 1)
        await pilot.press("enter")
        assert await _until(pilot, lambda: _top(app) == "View")
        assert app.is_running
        await pilot.press("escape")
        assert await _until(pilot, lambda: _top(app) == "SecurityScreen")
        assert "enter" in str(scr.query_one("#sec-foot").render())


def _everything(seen=None):
    """The routes of test_cli's two fake servers, plus the Rules and Calls tabs: every
    /security and /vms tab has a row to open."""
    from test_cli import _boxes_server, _security_server
    a, b = _boxes_server([]), _security_server([])
    extra = {
        "/api/permissions/rules": {"rules": [{"id": 3, "tool": "run_code", "prefix": "npm",
                                              "project_slug": None,
                                              "created_at": "2026-09-30 04:00:00"}]},
        "/api/logs/calls": {"hours": 24, "conversation_id": None,
                            "key_hosts": ["api.deepseek.com"],
                            "rows": [{"kind": "call", "id": 12, "ts": "2026-09-30 04:03:11",
                                      "model": "deepseek/deepseek-flash", "op_id": "7f3a",
                                      "box_id": "p-homelab", "conversation_id": 42,
                                      "project_slug": "homelab", "input_tokens": 12431,
                                      "output_tokens": 812, "cache_hit": 9102,
                                      "cache_miss": 3329, "cost_usd": 0.0021,
                                      "has_context": True}],
                            "truncated": False, "capture_context": True,
                            "totals": {"calls": 1, "cost_usd": 0.0021, "refused": 0}},
        "/api/logs/calls/12/context": {"messages": [{"role": "user", "content": "hi"}],
                                       "n_tools": 1, "input_tokens": 5, "cache_hit": 1,
                                       "cache_miss": 4},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.method, request.url.path))
        if request.url.path in extra and "jarvis_token=sess" in request.headers.get("cookie", ""):
            return httpx.Response(200, json=extra[request.url.path])
        r = a.handle_request(request)
        return r if r.status_code != 404 else b.handle_request(request)
    return httpx.MockTransport(handler)


async def _leave(pilot, app, scr, tries=6):
    """Close whatever dialog is open with esc until we are back on the screen."""
    for _ in range(tries):
        if app.screen is scr or not app.is_running:
            return
        await pilot.press("escape")
        await pilot.pause(0.15)


@pytest.mark.parametrize("cmd,tabs", [
    ("/security", ("queue", "network", "logs", "secrets", "persistent", "profiles", "rules",
                   "calls")),
    ("/vms", ("boxes", "images"))])
async def test_enter_on_the_first_row_of_every_tab_never_crashes(cmd, tabs):
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_everything())
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause(0.3)
        for tab in tabs:
            app.dispatch(f"{cmd} {tab}")
            assert await _until(pilot, lambda: _top(app) in ("SecurityScreen", "VmsScreen"))
            scr = app.screen
            assert await _until(pilot, lambda: scr.loaded.get(tab))
            await pilot.pause(0.15)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert app.is_running, tab
            await _leave(pilot, app, scr)
            assert app.screen is scr, f"{tab}: stuck on {_top(app)}"
            await pilot.press("escape")
            await pilot.pause(0.1)


# --- TUIB-03: every time on /security is this machine's, in one format ------------------------

def test_times_are_shown_in_the_local_zone(monkeypatch):
    _zone(monkeypatch, "America/Los_Angeles")
    assert jav3.local_ts("2026-09-30 04:56:59") == "09-29 21:56"
    assert jav3.local_ts("2026-09-30T04:56:59Z") == "09-29 21:56"
    assert jav3.local_ts(1_790_000_000) == jav3.local_ts(1_790_000_000.0) != ""
    assert jav3.full_ts("2026-09-30 04:56:59") == "2026-09-29 21:56:59"
    assert jav3.tz_label() == "PDT"
    assert jav3.row_ts("2026-09-30 04:56:59") == "09-29 21:56"
    assert jav3.row_ts("") == "" and jav3.full_ts(None) == "" and jav3.local_ts("soon") == ""
    assert jav3.full_ts("2026-09-20") == "2026-09-20"          # a date alone stays as sent
    _zone(monkeypatch, "UTC")
    assert jav3.tz_label() == "UTC"


async def test_security_rows_details_and_footer_use_local_time(monkeypatch):
    pytest.importorskip("textual")
    _zone(monkeypatch, "America/Los_Angeles")
    app = jav3.build_tui("http://h:1", SESSION, transport=_everything())
    async with app.run_test(size=(150, 45)) as pilot:
        scr = await _security(pilot, app, "logs")
        assert await _until(pilot, lambda: len(_rows(scr)) >= 2)
        # test_cli's alert #7 is 2026-09-25 10:05:00 UTC = 03:05 PDT
        row = next(r for r in _rows(scr) if "gate_flag" in r)
        assert "09-25 03:05" in row and "10:05" not in row
        scr.select_key("s7")
        detail = str(scr.query_one("#sec-detail").render())
        assert "2026-09-25 03:05:00" in detail
        assert "times PDT" in str(scr.query_one("#sec-foot").render())
        await pilot.press("4")                                   # Secrets: no times, no label
        await pilot.pause(0.2)
        assert "times PDT" not in str(scr.query_one("#sec-foot").render())


# --- TUIB-04: the selected row stays readable on the selection bar ------------------------------

def _content(row):
    from textual.content import Content
    c = row.content
    return Content.from_markup(c) if isinstance(c, str) else c


def _styles(row) -> list[str]:
    return [sp.style for sp in _content(row).spans if isinstance(sp.style, str)]


def _colours(styles) -> list[str]:
    """The foreground colour tokens on spans that have no background of their own."""
    return [t for s in styles if " on " not in f" {s} " for t in s.split() if t not in
            ("b", "bold", "i", "italic", "u", "underline", "strike")]


async def test_the_selected_security_row_drops_its_own_colours():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_everything())
    async with app.run_test(size=(150, 45)) as pilot:
        scr = await _security(pilot, app, "logs")
        assert await _until(pilot, lambda: len(_rows(scr)) >= 2)
        rows = list(scr.query("SecRow"))
        sel = next(r for r in rows if r.has_class("-sel"))
        other = next(r for r in rows if not r.has_class("-sel"))
        assert _colours(_styles(other))                       # unselected rows are coloured
        assert not _colours(_styles(sel)), _styles(sel)       # the bar's row is not
        assert "gate_flag" in _content(sel).plain or "host_cut" in _content(sel).plain
        await pilot.press("down")                             # moving hands the colours back
        assert _colours(_styles(sel)) and not _colours(_styles(
            next(r for r in scr.query("SecRow") if r.has_class("-sel"))))


async def test_the_selected_vms_box_row_keeps_its_bold_and_its_text():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_everything())
    async with app.run_test(size=(150, 45)) as pilot:
        scr = await _security(pilot, app, "boxes", cmd="/vms")
        assert await _until(pilot, lambda: len(_rows(scr)) >= 1)
        sel = next(r for r in scr.query("SecRow") if r.has_class("-sel"))
        assert "running" in _content(sel).plain or "stopped" in _content(sel).plain
        assert not _colours(_styles(sel)), _styles(sel)
