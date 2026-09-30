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

from cli_fake import load_client, wait_for

jav3 = load_client("jav3cli_tuib")

SESSION = "session:sess"


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
