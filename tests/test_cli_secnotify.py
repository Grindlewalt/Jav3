"""The terminal client's side of security notifications (F3, 2026-09-29).

The server decides what pings (backend/security.py). The client: follows one
multiplexed stream (/api/events, topics notices + security), turns a ping into
a sidebar notice with the status-row badge (critical: its own red count and a
red line in the chat when it lands during this chat's turn), ignores what does
not ping, and shows coalesced counts in /security."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

CLI = Path(__file__).resolve().parent.parent / "clients" / "jav3cli" / "jav3"


def _load():
    loader = importlib.machinery.SourceFileLoader("jav3cli_secnotify", str(CLI))
    spec = importlib.util.spec_from_loader("jav3cli_secnotify", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


jav3 = _load()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path / "cfg" / "jav3"


async def _until(pilot, cond, tries=80):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


def _text(widget) -> str:
    return str(widget.render())


def _sec(eid, kind, severity, summary, *, ping, tier, project=None, count=1, repeat=False,
         detail=None):
    return {"type": "security_event", "id": eid, "kind": kind, "severity": severity,
            "project": project, "summary": summary, "detail": detail, "count": count,
            "repeat": repeat, "tier": tier, "ping": ping}


LIVE = [
    _sec(1, "browser_session", "info", "browser 'Mac' connected", ping=False, tier="record"),
    _sec(2, "unexpected_process", "warn", "Unexpected process in box shared: /x",
         ping=False, tier="alert"),
    _sec(3, "package_requested", "info", "agent requested pip package left-pad",
         ping=True, tier="approval", project="demo"),
    _sec(4, "egress_anomaly", "critical", "volume spike to up.example (50000000 bytes)",
         ping=True, tier="critical", project="homelab"),
    {"type": "agent_run_done", "conversation_id": 44, "agent": "Coder", "ok": True,
     "took": "1m", "summary": "done"},
]

LOG = [{"id": 9, "kind": "unexpected_process", "severity": "warn", "project_slug": None,
        "summary": "Unexpected process in box shared: /usr/bin/evil", "detail": {"pid": 5},
        "acknowledged": 0, "created_at": "2026-09-28 10:00:00", "count": 3,
        "last_seen": "2026-09-28 13:00:00", "tier": "alert"}]


def _server(seen, live=LIVE, events_route=True):
    state = {"streams": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "jarvis_token=sess" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"detail": "not authenticated"})
        seen.append((request.method, path, str(request.url.query, "ascii")))
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "operator"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={
                "default": "deepseek/deepseek-flash", "active_project": None,
                "models": [{"id": "deepseek/deepseek-flash", "label": "Flash"}],
                "projects": [], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        if path == "/api/events" and events_route:
            state["streams"] += 1
            evs = live if state["streams"] == 1 else []      # a reconnect replays nothing
            frames = [{"topic": "security", "event": {"type": "stream_open"}}] + [
                {"topic": "notices" if e["type"] == "agent_run_done" else "security",
                 "event": e} for e in evs]
            return httpx.Response(200, text="".join(f"data: {json.dumps(f)}\n\n"
                                                    for f in frames),
                                  headers={"content-type": "text/event-stream"})
        if path == "/api/agents/notices/stream":
            return httpx.Response(200, text='data: {"type": "stream_open"}\n\n',
                                  headers={"content-type": "text/event-stream"})
        if path == "/api/security/events":
            return httpx.Response(200, json={"events": LOG})
        if path == "/api/projects":
            return httpx.Response(200, json={"projects": [], "active": None})
        if path == "/api/egress/pending":
            return httpx.Response(200, json={"pending": []})
        return httpx.Response(404, json={"detail": "nope"})
    return httpx.MockTransport(handler)


async def test_only_what_the_server_pings_reaches_the_sidebar(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server(seen))
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _until(pilot, lambda: any("egress_anomaly" in t for _, _, t in app.notices))
        await _until(pilot, lambda: any("Coder" in t for _, _, t in app.notices))
        # one stream for both topics
        assert ("GET", "/api/events", "topics=notices,security") in seen
        assert not any(p == "/api/agents/notices/stream" for _, p, _ in seen)
        kinds = {t.split(" ")[0]: k for _, k, t in app.notices if "Coder" not in t}
        assert kinds == {"egress_anomaly": "error", "package_requested": "warn"}
        crit = next(t for _, _, t in app.notices if t.startswith("egress_anomaly"))
        assert crit == "egress_anomaly ⌂ homelab volume spike to up.example (50000000 bytes)"
        assert app.crit_unread == 1 and app.unread == 3
        status = _text(app.query_one("#status-right"))
        assert "✗ 1" in status and "● 3" in status
        await pilot.press("ctrl+b")
        await pilot.pause(0.1)
        assert app.unread == 0 and app.crit_unread == 0
        notes = _text(app.query_one("#sb-notes"))
        assert "✗ egress_anomaly ⌂ homelab" in notes
        assert "browser_session" not in notes and "unexpected_process" not in notes


async def test_a_critical_alert_during_this_chats_turn_prints_in_the_chat(cfg):
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", "session:sess",
                         transport=_server([], live=[]))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.project, app.cid = "demo", 12
        app.busy, app.turn = True, jav3.TurnState()

        def lines():
            return [_text(n) for n in app.query("Note.error")]
        # another project's alert: sidebar only
        await app._security_notice(_sec(20, "egress_anomaly", "critical", "cut x.example",
                                        ping=True, tier="critical", project="other"))
        assert lines() == []
        # this project's, and one tied to this conversation: both in the chat
        await app._security_notice(_sec(21, "write_flag", "critical",
                                        "write refused (secret leak) in .env",
                                        ping=True, tier="critical", project="demo"))
        await app._security_notice(_sec(22, "proc_report_mismatch", "warn",
                                        "connection no process owns", ping=True,
                                        tier="critical", project="elsewhere",
                                        detail={"conversation_id": 12}))
        await pilot.pause(0.1)
        got = lines()
        assert len(got) == 2
        assert got[0].startswith("security: write_flag ⌂ demo write refused (secret leak)")
        assert "/security to review" in got[0]
        # a routine ping mid-turn stays out of the transcript
        await app._security_notice(_sec(23, "package_requested", "info", "pip x",
                                        ping=True, tier="approval", project="demo"))
        # an event from a server that does not stamp `ping`: critical only
        await app._security_notice({"type": "security_event", "id": 24, "kind": "gate_flag",
                                    "severity": "warn", "summary": "old warn"})
        await app._security_notice({"type": "security_event", "id": 25, "kind": "host_cut",
                                    "severity": "critical", "summary": "old crit"})
        await pilot.pause(0.1)
        texts = [t for _, _, t in app.notices]
        assert not any("old warn" in t for t in texts) and any("old crit" in t for t in texts)
        assert len(lines()) == 3                                   # the old crit, no project


async def test_an_older_server_falls_back_to_the_notices_stream(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess",
                         transport=_server(seen, events_route=False))
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _until(pilot, lambda: any(p == "/api/agents/notices/stream"
                                               for _, p, _ in seen))


async def test_security_log_rows_show_coalesced_counts(cfg):
    pytest.importorskip("textual")
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server([], live=[]))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/security logs")
        assert await _until(pilot, lambda: type(app.screen).__name__ == "SecurityScreen")
        scr = app.screen
        assert await _until(pilot, lambda: scr.entries["logs"])
        row = scr.row_markup(scr.entries["logs"][0])
        assert "[b]×3[/]" in row
        detail = scr.detail_markup(scr.entries["logs"][0])
        assert "seen 3× · last 2026-09-28 13:00:00" in detail
        q = scr.queue_entries([], {}, LOG)
        assert q[0]["ts"] == "2026-09-28 13:00:00"                # sorts by its latest repeat
