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
        if ("jarvis_token=sess" not in request.headers.get("cookie", "")
                and request.headers.get("authorization") != "Bearer jvd_x"):
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


# --- TUIB-05: enter never says yes to something that cannot be taken back --------------------

async def test_enter_declines_the_destroy_and_the_data_disk_prompts():
    pytest.importorskip("textual")
    from test_cli_vms import _server as vms_server
    seen: list = []
    tr, state = vms_server(seen)
    state["boxes"][1]["disk"] = {"overlay_bytes": 10_000_000, "data_bytes": 250_000_000}
    app = jav3.build_tui("http://h:1", SESSION, transport=tr)
    async with app.run_test(size=(150, 45)) as pilot:
        scr = await _security(pilot, app, "boxes", cmd="/vms")
        assert await _until(pilot, lambda: len(_rows(scr)) >= 2)
        scr.select_key("Xp-alpha")
        await pilot.press("d")
        assert await _until(pilot, lambda: _top(app) == "Confirm")
        keys = str(app.screen.query_one("#confirm-keys").render())
        assert "enter" in keys and "y" in keys
        await pilot.press("enter")                   # a habit: nothing is destroyed
        await pilot.pause(0.3)
        assert _top(app) == "VmsScreen"
        assert not [c for c in seen if c[0] == "POST"]
        await pilot.press("d")
        assert await _until(pilot, lambda: _top(app) == "Confirm")
        await pilot.press("y")                       # destroy it ...
        assert await _until(pilot, lambda: _top(app) == "Confirm"
                            and "data disk" in app.screen.question)
        await pilot.press("enter")                   # ... a second habitual enter keeps the disk
        assert await _until(pilot, lambda: any(
            c[0] == "POST" and c[1] == "/api/vm/boxes/p-alpha/destroy" for c in seen))
        post = next(c for c in seen if c[0] == "POST" and c[1].endswith("/destroy"))
        assert post[3] == {"confirm": True, "delete_data": False}


async def test_a_plain_confirm_still_takes_enter_and_says_so():
    pytest.importorskip("textual")
    from cli_fake import FakeServer, top
    srv = FakeServer(projects=["alpha"], full=True)
    app = jav3.build_tui("http://h:1", SESSION, transport=srv.transport())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.4)
        app.dispatch("/project brandnew")
        assert await wait_for(lambda: top(app) == "Confirm")
        assert "y / enter" in str(app.screen.query_one("#confirm-keys").render())
        await pilot.press("enter")
        assert await wait_for(lambda: srv.created == [{"name": "brandnew"}])


# --- TUIB-07: the detail pane scrolls with plain keys ----------------------------------------------

def _long_alert():
    ev = {"id": 798, "kind": "harness_fault", "severity": "warn", "project_slug": "demo",
          "summary": "the harness said something long", "acknowledged": 0,
          "created_at": "2026-09-30 04:56:59",
          "detail": json.dumps({"expected": " ".join(f"word{i}" for i in range(400))})}
    return ev


async def test_the_detail_pane_scrolls_with_plain_keys_and_the_footer_says_so():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv({
        "/api/security/events": {"events": [_long_alert()]}}))
    async with app.run_test(size=(80, 24)) as pilot:
        scr = await _security(pilot, app, "queue")
        assert await _until(pilot, lambda: len(_rows(scr)) >= 1)
        wrap = scr.query_one("#sec-detail-wrap")
        assert await _until(pilot, lambda: wrap.max_scroll_y > 0)
        assert await _until(pilot, lambda: "scroll the details" in str(
            scr.query_one("#sec-foot").render()))
        assert wrap.scroll_y == 0
        await pilot.press("right_square_bracket")
        await pilot.pause(0.2)
        assert wrap.scroll_y > 0
        seen = wrap.scroll_y
        await pilot.press("pagedown")                 # one row: the list fits, so the pane pages
        await pilot.pause(0.2)
        assert wrap.scroll_y > seen
        await pilot.press("left_square_bracket", "left_square_bracket", "left_square_bracket")
        await pilot.pause(0.2)
        assert wrap.scroll_y == 0


async def test_a_short_detail_does_not_advertise_scrolling():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv({
        "/api/security/events": {"events": [{**_long_alert(), "detail": None}]}}))
    async with app.run_test(size=(120, 40)) as pilot:
        scr = await _security(pilot, app, "queue")
        assert await _until(pilot, lambda: len(_rows(scr)) >= 1)
        await pilot.pause(0.3)
        assert "scroll the details" not in str(scr.query_one("#sec-foot").render())


# --- TUIB-08: the /vms and Network heads fit 80x24 ---------------------------------------------------

def _plain(markup: str) -> str:
    from textual.content import Content
    return Content.from_markup(markup).plain


def _wrapped(text: str, width: int) -> int:
    """Rows a text takes when each logical line wraps at `width`."""
    return sum(max(1, -(-len(ln) // width)) for ln in text.splitlines())


async def test_the_vms_head_fits_80x24_with_the_weak_isolation_badge_whole():
    pytest.importorskip("textual")
    from test_cli_vms import _server as vms_server
    tr, state = vms_server([])

    def weak(request):
        r = tr.handle_request(request)
        if request.url.path == "/api/vm/boxes" and r.status_code == 200:
            d = json.loads(r.content)
            d["runtimes"]["docker"] = {"available": True, "weak": True,
                                       "warnings": ["no gVisor: the container shares the host "
                                                    "kernel's full syscall surface",
                                                    "docker is neither rootless nor "
                                                    "userns-remapped: container uid 10001 is host "
                                                    "uid 10001"]}
            return httpx.Response(200, json=d)
        return r
    app = jav3.build_tui("http://h:1", SESSION, transport=httpx.MockTransport(weak))
    async with app.run_test(size=(80, 24)) as pilot:
        scr = await _security(pilot, app, "boxes", cmd="/vms")
        assert await _until(pilot, lambda: len(_rows(scr)) >= 2)
        sub = scr.query_one("#sec-sub")
        text = _plain(scr.sub_markup())
        assert "WEAK ISOLATION" in text and "docker" in text
        lines = text.splitlines()
        assert all(len(ln) <= 76 for ln in lines), lines        # no line wraps
        assert sub.size.height >= len(lines)                    # nothing cut off
        assert len(list(scr.query("SecRow"))) >= 2
        assert sum(1 for r in scr.query("SecRow") if r.region.height and
                   r.region.bottom <= scr.query_one("#sec-list").region.bottom) >= 3
        await pilot.resize_terminal(160, 48)                    # wide: the whole warnings return
        await pilot.pause(0.4)
        assert "neither rootless" in _plain(scr.sub_markup())


async def test_the_network_head_keeps_its_last_24h_line_at_80x24():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_everything())
    async with app.run_test(size=(80, 24)) as pilot:
        scr = await _security(pilot, app, "network")
        assert await _until(pilot, lambda: len(_rows(scr)) >= 2)
        text = _plain(scr.sub_markup())
        assert "last 24h" in text, text
        assert scr.query_one("#sec-sub").size.height >= _wrapped(text, 76), text


# --- TUIB-11: the sidebar never squeezes the chat to 38 columns ---------------------------------------

async def test_the_sidebar_waits_for_room():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)
        app.sidebar_pref = True                         # what the operator's tui.json says
        app.refresh_chrome()
        await pilot.pause(0.1)
        assert app.query_one("#sidebar").display is False
        assert app.query_one("#editor").region.width >= 70   # the whole width is the chat's
        seen: list = []
        app.notify = lambda msg, **kw: seen.append(msg)
        await pilot.press("ctrl+b")                     # says why, changes nothing
        assert seen and "100 columns" in seen[0] and app.sidebar_pref is True
        await pilot.resize_terminal(130, 30)
        await pilot.pause(0.3)
        assert app.query_one("#sidebar").display is True
        await pilot.resize_terminal(80, 24)
        await pilot.pause(0.3)
        assert app.query_one("#sidebar").display is False


async def test_a_notice_while_the_sidebar_waits_still_counts_as_unread():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_srv())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)
        app.sidebar_pref = True
        app.refresh_chrome()
        app.push_notice("something finished", toast=False)
        assert app.unread == 1


# --- TUIB-10: the profile form keeps its Save row and its hint on a 24-row terminal --------------

async def test_the_profile_form_fits_80x24_and_follows_the_cursor():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION,
                         transport=_srv({"/api/profiles": {"profiles": [PROFILE]}}))
    async with app.run_test(size=(80, 24)) as pilot:
        scr = await _security(pilot, app, "profiles")
        assert await _until(pilot, lambda: len(_rows(scr)) >= 1)
        await pilot.press("a")
        assert await _until(pilot, lambda: _top(app) == "ProfileForm")
        form = app.screen
        await pilot.pause(0.3)
        dlg, hint = form.query_one("#dialog"), form.query_one("#pf-hint")
        body = form.query_one("#view-body")
        assert hint.region.height >= 1 and hint.region.bottom <= dlg.region.bottom
        assert "ctrl+s" in str(hint.render())
        assert body.region.bottom <= hint.region.y                  # the body never covers it
        n = len(form.FIELDS)
        for _ in range(n):                                          # down to the Save row
            await pilot.press("down")
        await pilot.pause(0.3)
        assert form.cur == n
        save_line = n + 1
        assert body.scroll_y <= save_line < body.scroll_y + body.size.height, (
            body.scroll_y, body.size.height)
        for _ in range(n):                                          # and back up to the top
            await pilot.press("up")
        await pilot.pause(0.3)
        assert body.scroll_y == 0
        await pilot.press("escape")


# --- TUIB-09: logged out and chat-only read differently, once ----------------------------------------

async def test_logged_out_says_not_logged_in_once_and_enter_offers_the_login():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", None, transport=_srv())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/security")
        assert await _until(pilot, lambda: _top(app) == "SecurityScreen")
        scr = app.screen
        await pilot.pause(0.2)
        sub = str(scr.query_one("#sec-sub").render())
        assert "not logged in" in sub and "device token" not in sub
        assert sub.count("logged in") == 1
        assert not any("logged in" in r and "not logged in" in r for r in _rows(scr))
        assert "device token" not in " ".join(_rows(scr))
        await pilot.press("enter")                    # offers the password login
        assert await _until(pilot, lambda: _top(app) == "Ask")
        assert "Server address" in str(app.screen.query_one("Static").render())
        await pilot.press("escape")
        assert await _until(pilot, lambda: _top(app) == "SecurityScreen")


async def test_a_chat_only_token_is_told_so():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_srv())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/vms")
        assert await _until(pilot, lambda: _top(app) == "VmsScreen")
        await pilot.pause(0.2)
        sub = str(app.screen.query_one("#sec-sub").render())
        assert "chat only" in sub and "full access" in sub and "not logged in" not in sub


# --- TUIB-12: a server that cannot be reached never reads as an all-clear -----------------------

def _down_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/api/devices/whoami", "/api/chat/options", "/api/conversations"):
            return httpx.Response(200, json={"username": "op", "default": "x", "models": [],
                                             "projects": [], "agents": [], "conversations": []})
        raise httpx.ConnectError("All connection attempts failed")
    return httpx.MockTransport(handler)


async def test_an_unreachable_server_is_not_an_all_clear():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_down_transport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/security")
        assert await _until(pilot, lambda: _top(app) == "SecurityScreen")
        scr = app.screen
        assert await _until(pilot, lambda: scr.loaded["queue"] and scr.loaded["secrets"])
        await pilot.pause(0.2)
        sub = _plain(scr.sub_markup())
        assert "could not load" in sub and "r retries" in sub
        assert "0 hosts waiting" not in sub
        assert sub.count("could not reach") == 1, sub          # one line for the shared cause
        assert "projects" in sub and "egress" in sub           # naming the calls that failed
        assert "nothing waits on you" not in " ".join(_rows(scr))
        assert any("could not load" in r for r in _rows(scr))
        tabs = str(scr.query_one("#sec-tab-queue").render())
        assert "?" in tabs and "Queue 0" not in tabs
        assert "?" in str(scr.query_one("#sec-tab-secrets").render())
        assert _wrapped(sub, 76) <= scr.query_one("#sec-sub").size.height


def test_collapse_errors_names_the_calls_that_share_a_cause():
    out = jav3.collapse_errors(["projects: could not reach http://h:1: nothing answers there",
                                "egress: could not reach http://h:1: nothing answers there",
                                "alerts: the server said 500"])
    assert out == ["could not reach http://h:1: nothing answers there (projects, egress)",
                   "alerts: the server said 500"]
    assert jav3.collapse_errors([]) == []


# --- TUIB-06: a name typed into a picker keeps its first letter, t included ------------------------

async def test_a_picker_keeps_the_first_letter_when_it_is_t():
    pytest.importorskip("textual")
    from cli_fake import FakeServer
    srv = FakeServer(projects=["alpha"], full=True)
    app = jav3.build_tui("http://h:1", SESSION, transport=srv.transport())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.4)
        app.dispatch("/project")
        assert await wait_for(lambda: _top(app) == "Picker")
        hint = str(app.screen.query_one("#dialog-hint").render())
        assert "type to filter" in hint and "t type" not in hint
        await pilot.press(*"tetris")
        f = app.screen.query_one("#filter")
        assert f.value == "tetris"
        await pilot.press("enter")
        assert await wait_for(lambda: _top(app) == "Confirm")
        assert "'tetris'" in app.screen.question
        await pilot.press("n")


async def test_the_selected_vms_box_row_keeps_its_bold_and_its_text():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", SESSION, transport=_everything())
    async with app.run_test(size=(150, 45)) as pilot:
        scr = await _security(pilot, app, "boxes", cmd="/vms")
        assert await _until(pilot, lambda: len(_rows(scr)) >= 1)
        sel = next(r for r in scr.query("SecRow") if r.has_class("-sel"))
        assert "running" in _content(sel).plain or "stopped" in _content(sel).plain
        assert not _colours(_styles(sel)), _styles(sel)
