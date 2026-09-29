"""The Calls tab of /security in the terminal client (clients/jav3cli/jav3): the
pure row / detail / header markup, and the tab itself against a fake server."""
import importlib.machinery
import importlib.util
from pathlib import Path

import httpx
import pytest

CLI = Path(__file__).resolve().parent.parent / "clients" / "jav3cli" / "jav3"


def _load():
    loader = importlib.machinery.SourceFileLoader("jav3cli_calls", str(CLI))
    spec = importlib.util.spec_from_loader("jav3cli_calls", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


jav3 = _load()

CALL = {"kind": "call", "id": 12, "ts": "2026-09-27 14:03:11", "model": "deepseek/deepseek-flash",
        "op_id": "7f3a", "box_id": "p-homelab", "conversation_id": 42,
        "project_slug": "homelab", "input_tokens": 12431, "output_tokens": 812,
        "cache_hit": 9102, "cache_miss": 3329, "cost_usd": 0.0021, "has_context": True}
REFU = {"kind": "refused", "id": 3, "ts": "2026-09-27 13:40:00", "op_name": "model_call",
        "reason": "unknown_op_id", "box_id": "p-homelab", "project_slug": "homelab"}


def _e(row):
    return {"type": "refusal" if row["kind"] == "refused" else "call",
            "key": f"x{row['id']}", "ts": row["ts"], "raw": row}


# --- the pure helpers ------------------------------------------------------

def test_token_and_money_forms():
    assert [jav3.calls_tok(n) for n in (0, 812, 999, 1000, 12431, 9102, 1_200_000, None)] == \
        ["0", "812", "999", "1.0k", "12.4k", "9.1k", "1.2M", "0"]
    assert jav3.calls_usd(0.0021) == "$0.0021"
    assert jav3.calls_usd(0) == "$0.0000"
    assert jav3.calls_usd(0.41, 2) == "$0.41"
    assert jav3.calls_usd(12.5) == "$12.50"
    assert jav3.calls_model("deepseek/deepseek-flash") == "deepseek-flash"
    assert jav3.calls_model("openrouter/x/y") == "openrouter/x/y"


def test_call_row_is_the_specified_string():
    assert jav3.calls_row(_e(CALL)) == (
        "[$primary]●[/] [$text-muted]09-27 14:03[/] [$primary b]CALL[/] deepseek-flash "
        "[$text-muted]op 7f3a ⌂ homelab #42[/] ↑12.4k ↓812 [$text-muted]cache 9.1k[/] $0.0021")


def test_refused_row_is_the_specified_string():
    assert jav3.calls_row(_e(REFU)) == (
        "[$error]●[/] [$text-muted]09-27 13:40[/] [$error b]REFU[/] model_call "
        "unknown_op_id [$text-muted]from box p-homelab[/]")


def test_call_row_leaves_out_what_the_row_lacks():
    bare = {**CALL, "op_id": None, "project_slug": None, "conversation_id": None}
    row = jav3.calls_row(_e(bare))
    assert "op " not in row and "⌂" not in row and "#" not in row
    assert "deepseek-flash ↑12.4k" in row
    assert "from box -" in jav3.calls_row(_e({**REFU, "box_id": None}))


def test_rows_escape_what_a_guest_typed():
    # the op name and reason are guest-supplied text: no markup may come through
    row = jav3.calls_row(_e({**REFU, "op_name": "[b]x", "reason": "[/]"}))
    assert "\\[b]x" in row and "\\[/]" in row


def test_call_detail_matches_the_spec():
    d = jav3.calls_detail(_e(CALL)).splitlines()
    assert d[0] == "op 7f3a · conversation #42 · box p-homelab · deepseek-flash"
    assert d[1] == "12,431 in (9,102 cached) · 812 out · $0.0021 · context stored"
    assert d[-1] == "[$primary]enter[/] view context   [$primary]c[/] this conversation only"


def test_call_detail_without_context_says_so():
    d = jav3.calls_detail(_e({**CALL, "has_context": False}))
    assert "context not stored" in d and "no context stored for this call" in d
    assert "view context" not in d


def test_refusal_detail_explains_the_reason():
    d = jav3.calls_detail(_e(REFU))
    assert "REFUSED" in d and "model_call" in d and "from box p-homelab" in d
    assert "nothing reached the provider" in d
    other = jav3.calls_detail(_e({**REFU, "reason": "something_new"}))
    assert "something_new" in other


def test_header_names_the_host_and_the_window():
    meta = {"key_hosts": ["api.deepseek.com"], "hours": 24, "truncated": False,
            "totals": {"calls": 214, "cost_usd": 0.41, "refused": 2}}
    head = jav3.calls_sub(meta)
    assert head.splitlines() == [
        "[$text-muted]key held by the host gateway · sent only to api.deepseek.com · the "
        "guest never sees it[/]",
        "last 24h: 214 calls · $0.41 · [$error]2 refused[/]"]
    quiet = jav3.calls_sub({**meta, "totals": {"calls": 1, "cost_usd": 0.0021, "refused": 0}})
    assert quiet.endswith("last 24h: 1 calls · $0.0021 · 0 refused")
    mine = jav3.calls_sub(meta, only=42)
    assert "conversation #42" in mine.splitlines()[1] and "refused" not in mine
    assert "no model key is set" in jav3.calls_sub({**meta, "key_hosts": []})
    assert jav3.calls_sub({}).count("\n") == 0                  # before the first load


def test_context_view_is_bounded():
    payload = {"input_tokens": 100, "cache_hit": 40, "n_tools": 3,
               "messages": [{"role": "system", "content": "be brief"},
                            {"role": "user", "content": [{"type": "text", "text": "hi [x]"},
                                                         {"type": "image_url"}]},
                            {"role": "assistant", "content": None,
                             "tool_calls": [{"function": {"name": "web_read",
                                                          "arguments": '{"url": "u"}'}}]},
                            {"role": "user", "content": "z" * 50_000}]}
    out = jav3.calls_context_markup(payload, cap=100, total=10_000)
    assert "100 in (40 cached) · 4 messages · 3 tools offered" in out
    assert "be brief" in out and "hi \\[x]" in out and "web_read" in out
    assert "more characters" in out and len(out) < 2_000
    many = jav3.calls_context_markup({"messages": [{"role": "user", "content": "a" * 60}] * 50},
                                     total=200)
    assert "more messages not shown" in many


# --- the tab, against a fake server ----------------------------------------

def _server(seen, rows=None, capture=True):
    rows = list(rows if rows is not None else [CALL, {**CALL, "id": 11, "op_id": "7f39",
                                                       "has_context": False,
                                                       "ts": "2026-09-27 14:02:00"}, REFU])

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if "jarvis_token=sess" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"detail": "not authenticated"})
        seen.append((method, path, dict(request.url.params)))
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "operator"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={"default": "deepseek/deepseek-flash",
                                             "models": [], "projects": [], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        if path == "/api/agents/notices/stream":
            return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})
        if path == "/api/logs/calls":
            cid = request.url.params.get("conversation_id")
            shown = ([r for r in rows if str(r.get("conversation_id")) == cid and
                      r["kind"] == "call"] if cid else rows)
            calls = [r for r in shown if r["kind"] == "call"]
            return httpx.Response(200, json={
                "hours": 24, "conversation_id": int(cid) if cid else None,
                "key_hosts": ["api.deepseek.com"], "rows": shown, "truncated": False,
                "capture_context": capture,
                "totals": {"calls": len(calls), "cost_usd": 0.0035 * len(calls),
                           "refused": 0 if cid else sum(r["kind"] == "refused" for r in rows)}})
        if path == "/api/logs/calls/12/context":
            return httpx.Response(200, json={
                "messages": [{"role": "user", "content": "what is the plan"}], "n_tools": 2,
                "input_tokens": 12431, "cache_hit": 9102, "cache_miss": 3329})
        if path in ("/api/projects", "/api/egress/pending", "/api/security/events",
                    "/api/services", "/api/packages", "/api/secrets"):
            return httpx.Response(200, json={"projects": [], "pending": [], "events": [],
                                             "services": [], "packages": [], "secrets": []})
        return httpx.Response(404, json={"detail": "nope"})
    return httpx.MockTransport(handler)


async def _until(pilot, cond, tries=60):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


def _rows(scr):
    return [str(r.render()) for r in scr.query("SecRow")]


async def _open(pilot, app, cid=42):
    await pilot.pause(0.3)
    app.cid = cid
    app.dispatch("/security calls")
    assert await _until(pilot, lambda: type(app.screen).__name__ == "SecurityScreen")
    scr = app.screen
    assert await _until(pilot, lambda: scr.loaded["calls"] and len(_rows(scr)) >= 1)
    await pilot.pause(0.1)
    return scr


async def test_calls_tab_lists_calls_and_refusals_with_the_header():
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        scr = await _open(pilot, app)
        assert scr.tab == "calls" and scr.TABS[-1] == "calls" and scr.TABS.index("calls") == 7
        rows = _rows(scr)
        assert len(rows) == 3
        assert "CALL" in rows[0] and "deepseek-flash" in rows[0] and "op 7f3a" in rows[0]
        assert "⌂ homelab #42" in rows[0] and "↑12.4k ↓812" in rows[0] and "$0.0021" in rows[0]
        assert "REFU" in rows[2] and "unknown_op_id" in rows[2] and "from box p-homelab" in rows[2]
        sub = str(scr.query_one("#sec-sub").render())
        assert "sent only to api.deepseek.com" in sub and "the guest never sees it" in sub
        assert "last 24h: 2 calls" in sub and "1 refused" in sub
        foot = str(scr.query_one("#sec-foot").render())
        assert "c" in foot and "conversation" in foot and "enter" in foot and "context" in foot
        assert "1-8" in foot
        detail = str(scr.query_one("#sec-detail").render())
        assert "12,431 in (9,102 cached)" in detail and "conversation #42" in detail
        await pilot.press("down", "down")
        detail = str(scr.query_one("#sec-detail").render())
        assert "REFUSED" in detail and "nothing reached the provider" in detail
        assert not any("sk-" in r for r in rows)


async def test_calls_tab_is_reachable_by_8_and_by_tabbing():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server([]))
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/security")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "SecurityScreen")
        scr = app.screen
        await pilot.press("8")
        assert scr.tab == "calls" and scr.query_one("#sec-tab-calls").has_class("-on")
        await pilot.press("tab")
        assert scr.tab == "queue"                     # wraps round


async def test_c_toggles_this_conversation_only():
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        scr = await _open(pilot, app, cid=42)
        assert not any(p.get("conversation_id") for m, path, p in seen if path == "/api/logs/calls")
        await pilot.press("c")
        assert await _until(pilot, lambda: any(p.get("conversation_id") == "42"
                                               for m, path, p in seen if path == "/api/logs/calls"))
        assert await _until(pilot, lambda: len(_rows(scr)) == 2)
        assert all("REFU" not in r for r in _rows(scr))
        assert "conversation #42" in str(scr.query_one("#sec-sub").render())
        await pilot.press("c")                        # and back to everything
        assert await _until(pilot, lambda: len(_rows(scr)) == 3)
        assert scr.calls_only is False


async def test_c_without_a_conversation_says_so_and_asks_nothing_more():
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        scr = await _open(pilot, app, cid=None)
        n = len(seen)
        await pilot.press("c")
        await pilot.pause(0.2)
        assert "no conversation to filter by" in str(scr.query_one("#sec-sub").render())
        assert scr.calls_only is False
        assert not any(p.get("conversation_id") for m, path, p in seen[n:])


async def test_enter_views_the_captured_context():
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        scr = await _open(pilot, app)
        await pilot.press("enter")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "View")
        assert any(path == "/api/logs/calls/12/context" for m, path, p in seen)
        body = " ".join(str(w.render()) for w in app.screen.query("Static"))
        assert "what is the plan" in body and "12,431 in (9,102 cached)" in body
        await pilot.press("escape")
        assert await _until(pilot, lambda: app.screen is scr)


async def test_enter_on_a_call_without_context_only_notes_it():
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        scr = await _open(pilot, app)
        await pilot.press("down", "enter")
        await pilot.pause(0.2)
        assert app.screen is scr
        assert "context wasn't captured" in str(scr.query_one("#sec-sub").render())
        assert not any("/context" in path for m, path, p in seen)


async def test_empty_and_locked():
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server([], rows=[]))
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/security calls")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "SecurityScreen")
        scr = app.screen
        assert await _until(pilot, lambda: scr.loaded["calls"])
        await pilot.pause(0.1)
        assert _rows(scr) == ["no model calls yet"]
    seen: list = []
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/security calls")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "SecurityScreen")
        await pilot.pause(0.3)
        assert not any(path == "/api/logs/calls" for m, path, p in seen)   # chat-only login
