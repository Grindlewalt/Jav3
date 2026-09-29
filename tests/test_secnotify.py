"""Security notifications: fewer pings, same record (F3, 2026-09-29).

Each accuracy fix has a pair: the normal case the Pi's log showed firing no
longer fires, and the bad case the detector exists for still does. Then the
volume side: coalescing, tiers, the operator's level, the per-kind rate
limit, and what /api/notifications counts."""
from types import SimpleNamespace

import httpx
import pytest

from backend import anomaly, bus, diffgate, egress, security
from backend import db as db_mod
from backend.auth import hash_password
from backend.config import settings
from backend.main import app
from backend.vm import procview


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    egress._cut.clear()
    security._pings.clear()
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


@pytest.fixture
def feed():
    """Everything published on the security channel while the test runs."""
    q = bus.subscribe(security.SECURITY_CHAN)
    got: list[dict] = []

    def drain():
        while not q.empty():
            got.append(q.get_nowait())
        return got
    yield drain
    bus.unsubscribe(security.SECURITY_CHAN, q)


async def _rows(db):
    async with db.execute("SELECT id, kind, severity, count, last_seen, cause, acknowledged "
                          "FROM security_events ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


# --- accuracy: the egress volume detector ------------------------------------------

async def _hit(db, host, when, out, slug="startup"):
    await db.execute("INSERT INTO egress_events(project_slug, host, bytes_out, verdict, "
                     "created_at) VALUES (?,?,?,'allow',?)", (slug, host, out, when))
    await db.commit()


async def test_daily_pip_session_is_not_a_volume_spike(db):
    """The Pi's one egress_anomaly (2026-09-07): a daily job's 25 KB of TLS to
    files.pythonhosted.org, summed over 40 days, crossed 1 MB and >8x the
    median of the project's other hosts, and the CDN was cut."""
    for day in range(40):
        when = f"2026-08-{day % 28 + 1:02d} 06:04:33" if day < 28 else \
            f"2026-09-{day - 27:02d} 06:04:33"
        await _hit(db, "files.pythonhosted.org", when, 25_087)
    for h in ("pypi.org", "api.github.com", "example.com"):
        await _hit(db, h, "2026-09-12 06:00:00", 20_000)
    assert await anomaly.check_host(db, "startup", "files.pythonhosted.org") is None


async def test_an_upload_inside_the_window_still_trips(db):
    for h in ("a.com", "b.com", "c.com"):
        await _hit(db, h, "2026-09-12 05:00:00", 1000)
    await _hit(db, "files.pythonhosted.org", "2026-09-12 05:30:00", 25_000)
    await _hit(db, "files.pythonhosted.org", "2026-09-12 06:00:00", 3_000_000)
    a = await anomaly.check_host(db, "startup", "files.pythonhosted.org")
    assert a and a["kind"] == "volume_spike" and a["detail"]["bytes_out"] == 3_025_000


async def test_a_daily_schedule_is_not_a_beacon(db):
    for day in range(1, 15):
        await _hit(db, "api.weather.example", f"2026-09-{day:02d} 07:00:00", 900)
    assert await anomaly.check_host(db, "startup", "api.weather.example") is None


async def test_a_fast_beacon_still_trips(db):
    for i in range(8):
        await _hit(db, "c2.example", f"2026-09-12 12:{i * 5:02d}:00", 100)
    a = await anomaly.check_host(db, "startup", "c2.example")
    assert a and a["kind"] == "beacon_cadence"


# --- accuracy: diffgate new_import -------------------------------------------------

def _mods(new, path):
    flags = diffgate.scan("", new, path)
    f = next((f for f in flags if f["trigger"] == "new_import"), None)
    return f["detail"]["modules"] if f else []


def test_relative_stdlib_and_builtin_imports_do_not_flag():
    js = ("import { test } from 'node:test'\nimport assert from 'node:assert/strict'\n"
          "import test2 from 'node:test'\nimport fs from 'fs'\n"
          "import { WaveField } from './WaveField.js'\nimport x from '../lib/x.mjs'\n")
    assert _mods(js, "tests/blocks.test.mjs") == []
    py = "import os\nimport sys, json\nfrom typing import Any\nfrom . import util\nfrom .m import y\n"
    assert _mods(py, "tool.py") == []


def test_third_party_and_network_imports_still_flag():
    js = ("import axios from 'axios'\nconst cp = require('child_process')\n"
          "import https from 'node:https'\nimport { x } from './local.js'\n")
    assert _mods(js, "src/main.js") == ["axios", "child_process", "node:https"]
    py = "import maigret\nimport socket\nfrom urllib.request import urlopen\nimport os\n"
    assert _mods(py, "osint.py") == ["maigret", "socket", "urllib"]


def test_python_pattern_no_longer_reads_js_default_imports():
    # `import test from 'node:test'` used to add a Python module named `test`
    assert _mods("import test from 'left-pad'\n", "a.mjs") == ["left-pad"]


def test_other_file_types_keep_the_old_behaviour():
    # a shell or YAML file is not parsed as either language: nothing filtered
    assert _mods("import os\n", "setup.sh") == ["os"]


# --- accuracy: the process baseline ------------------------------------------------

def _snap(*procs):
    raw = {"boot_id": "b1", "self_pid": 400, "uptime_s": 100.0, "procs": [
        {"pid": 400, "ppid": 1, "exe": "/usr/bin/python3", "unit": "jarvis-guest.service"},
        *procs], "socks": []}
    return procview.sanitize_snapshot(raw)


def _unexpected(snap, kind="shared"):
    box = SimpleNamespace(id="shared", kind=kind, project=None)
    tags = procview.classify(snap, box, procview.BUILTIN, None)
    return sorted(p for p, t in tags.items() if t == "unexpected")


def test_debian_timer_units_are_os():
    snap = _snap(
        {"pid": 1, "ppid": 0, "exe": "/usr/lib/systemd/systemd", "unit": "init.scope"},
        {"pid": 1714, "ppid": 1, "exe": "/usr/bin/python3.13", "unit": "apt-listchanges.service"},
        {"pid": 1715, "ppid": 1714, "exe": "/usr/bin/apt-get", "unit": "apt-listchanges.service"},
        {"pid": 1716, "ppid": 1715, "exe": "/usr/lib/apt/methods/http",
         "unit": "apt-listchanges.service"},
        {"pid": 1800, "ppid": 1, "exe": "/usr/sbin/fstrim", "unit": "fstrim.service"},
        {"pid": 1801, "ppid": 1, "exe": "/usr/bin/mandb", "unit": "man-db.service"})
    assert _unexpected(snap) == []


def test_the_same_binaries_elsewhere_are_still_unexpected():
    snap = _snap(
        {"pid": 1, "ppid": 0, "exe": "/usr/lib/systemd/systemd", "unit": "init.scope"},
        {"pid": 900, "ppid": 1, "exe": "/usr/bin/python3.13", "unit": "listchanges.service"},
        {"pid": 901, "ppid": 1, "exe": "/usr/bin/apt-get", "unit": "evil.service"})
    assert _unexpected(snap) == [900, 901]


def test_a_docker_boxes_init_is_os_but_a_second_tini_is_not():
    snap = _snap(
        {"pid": 1, "ppid": 0, "exe": "/usr/bin/tini", "unit": ""},
        {"pid": 57, "ppid": 1, "exe": "/usr/bin/tini", "unit": ""})
    assert _unexpected(snap, kind="project") == [57]


# --- tiers and levels --------------------------------------------------------------

def test_tiers():
    t = security.tier
    assert t("write_flag", "critical") == "critical"             # a refused secret leak
    assert t("proc_report_mismatch", "warn") == "critical"       # hidden connection
    assert t("egress_anomaly", "warn") == "critical"
    assert t("package_requested", "info") == "approval"
    assert t("service_requested", "info") == "approval"
    assert t("unexpected_process", "warn") == "alert"
    assert t("browser_session", "info") == "record"
    assert [security.wants("critical", lv) for lv in security.LEVELS] == [True] * 3
    assert [security.wants("approval", lv) for lv in security.LEVELS] == [False, True, True]
    assert [security.wants("alert", lv) for lv in security.LEVELS] == [False, False, True]
    assert [security.wants("record", lv) for lv in security.LEVELS] == [False] * 3


async def test_default_level_pings_criticals_and_approvals_only(db, feed):
    await security.raise_event(db, kind="host_cut", summary="cut evil.com", severity="critical")
    await security.raise_event(db, kind="package_requested", summary="pip x", severity="info")
    await security.raise_event(db, kind="unexpected_process", summary="odd", severity="warn")
    await security.raise_event(db, kind="browser_session", summary="connected", severity="info")
    got = {e["kind"]: e["ping"] for e in feed()}
    assert got == {"host_cut": True, "package_requested": True,
                   "unexpected_process": False, "browser_session": False}
    assert len(await _rows(db)) == 4                              # all recorded


async def test_levels_change_what_pings(db, feed):
    await security.set_notify_level(db, "critical")
    await security.raise_event(db, kind="package_requested", summary="p1", severity="info")
    await security.set_notify_level(db, "all")
    await security.raise_event(db, kind="unexpected_process", summary="u1", severity="warn")
    await security.raise_event(db, kind="browser_session", summary="b1", severity="info")
    assert [e["ping"] for e in feed()] == [False, True, False]
    with pytest.raises(ValueError):
        await security.set_notify_level(db, "loud")


# --- coalescing ----------------------------------------------------------------------

async def test_a_repeat_counts_onto_the_unacknowledged_row(db, feed):
    kw = dict(kind="unexpected_process", severity="warn", project=None,
              summary="Unexpected process in box shared: /usr/bin/apt-get")
    a = await security.raise_event(db, **kw, detail={"pid": 1})
    b = await security.raise_event(db, **kw, detail={"pid": 2})
    assert a == b
    rows = await _rows(db)
    assert len(rows) == 1 and rows[0]["count"] == 2 and rows[0]["last_seen"]
    evs = feed()
    assert [e["repeat"] for e in evs] == [False, True] and evs[1]["count"] == 2
    listed = (await security.list_events(db))[0]
    assert listed["count"] == 2 and listed["tier"] == "alert"
    assert listed["detail"] == {"pid": 1}                          # the first evidence stays


async def test_acknowledged_or_different_is_a_new_row(db):
    kw = dict(kind="docker_weak_isolation", summary="no gVisor", project="docker-a")
    first = await security.raise_event(db, **kw, severity="warn")
    await security.acknowledge(db, first)
    second = await security.raise_event(db, **kw, severity="warn")
    assert second != first                                         # the operator saw the first
    third = await security.raise_event(db, **kw, severity="critical")
    other = await security.raise_event(db, **{**kw, "project": "docker-b"}, severity="warn")
    assert len({first, second, third, other}) == 4
    assert await security.raise_event(db, **kw, severity="warn") == second


async def test_a_repeat_outside_the_window_is_a_new_row(db, monkeypatch):
    kw = dict(kind="login_failed", summary="burst for 'root'", severity="warn")
    a = await security.raise_event(db, **kw)
    await db.execute("UPDATE security_events SET created_at = datetime('now', '-2 days'), "
                     "last_seen = datetime('now', '-2 days') WHERE id = ?", (a,))
    await db.commit()
    assert await security.raise_event(db, **kw) != a
    monkeypatch.setattr(settings, "security_coalesce_seconds", 0)
    n = len(await _rows(db))
    await security.raise_event(db, **kw)
    assert len(await _rows(db)) == n + 1                           # 0 turns coalescing off


async def test_a_critical_repeat_pings_again_once_per_window(db, feed, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(security, "_clock", lambda: now["t"])
    kw = dict(kind="write_flag", severity="critical", project="p",
              summary="write refused (secret leak) in .env")
    for step in (0, 5, 5, settings.security_ping_window_seconds, 5):
        now["t"] += step
        await security.raise_event(db, **kw)
    # the first pings; repeats inside the window are counted quietly; the
    # first repeat after it pings again (the card shows the count)
    assert [(e["repeat"], e["ping"], e["count"]) for e in feed()] == [
        (False, True, 1), (True, False, 2), (True, False, 3), (True, True, 4),
        (True, False, 5)]


# --- the per-kind rate limit ---------------------------------------------------------

async def test_non_critical_pings_are_rate_limited_per_kind(db, feed, monkeypatch):
    monkeypatch.setattr(settings, "security_ping_per_kind", 3)
    await security.set_notify_level(db, "all")
    for i in range(5):
        await security.raise_event(db, kind="desk_refused", summary=f"r{i}", severity="warn")
    await security.raise_event(db, kind="browser_refused", summary="other kind", severity="warn")
    for i in range(5):
        await security.raise_event(db, kind="egress_anomaly", summary=f"cut {i}",
                                   severity="critical")
    pings = [(e["kind"], e["ping"]) for e in feed()]
    assert pings[:6] == [("desk_refused", True)] * 3 + [("desk_refused", False)] * 2 \
        + [("browser_refused", True)]
    assert pings[6:] == [("egress_anomaly", True)] * 5             # never rate limited
    assert len(await _rows(db)) == 11                               # and all recorded


# --- /api/notifications -------------------------------------------------------------

@pytest.fixture
async def client(tmp_env):
    await db_mod.init_db()
    security._pings.clear()
    conn = await db_mod.get_db()
    try:
        await conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                           ("operator", hash_password("hunter2")))
        await conn.commit()
    finally:
        await conn.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        yield c


async def test_records_leave_the_badge_but_not_the_log(client):
    conn = await db_mod.get_db()
    try:
        for i in range(4):
            await security.raise_event(conn, kind="browser_session", summary=f"s{i}",
                                       severity="info")
        await security.raise_event(conn, kind="unexpected_process", summary="u", severity="warn")
        await security.raise_event(conn, kind="host_cut", summary="c", severity="critical")
    finally:
        await conn.close()
    body = (await client.get("/api/notifications")).json()
    assert body["count"] == 2 and body["alerts"] == 2 and body["critical"] == 1
    assert body["records"] == 4 and body["ping_count"] == 1 and body["level"] == "approvals"
    evs = (await client.get("/api/security/events?unacknowledged=true")).json()["events"]
    assert len(evs) == 6                                            # the queue still has all


async def test_the_level_setting_round_trips(client):
    assert (await client.get("/api/notifications/settings")).json() == {
        "level": "approvals", "levels": ["critical", "approvals", "all"]}
    r = await client.put("/api/notifications/settings", json={"level": "all"})
    assert r.status_code == 200 and r.json()["level"] == "all"
    assert (await client.get("/api/notifications")).json()["level"] == "all"
    assert (await client.put("/api/notifications/settings",
                             json={"level": "loud"})).status_code == 400


async def test_settings_need_a_session():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.put("/api/notifications/settings",
                            json={"level": "all"})).status_code == 401
