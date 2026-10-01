"""Security false alarms: unexpected_process (SB1, 2026-10-01).

On the Pi 172 of 186 were stock Debian units (apt-listchanges 163, fstrim,
man-db) and docker's tini; the rest were a Chromium the agent's screenshot
started and a node server its run_code left running. Each is now judged:

  * every .service the image's distribution ships is baseline (`units_vendor`,
    recorded at image build), a Docker box's tini is the OS
  * a process the agent's run_code started and left running is RECORDED, filed
    quietly ("started by the agent"), once per (boot, exe, script)
  * a unit that is not in the image, or any other process, still alerts"""
import pytest

from backend import security
from backend.config import settings
from backend.vm import boxes, procview

BOOT = "5b0c6a1e-0000-4000-8000-000000000001"


def _p(pid, ppid, exe, cmd, unit="", comm=None, user="jav3"):
    return {"pid": pid, "ppid": ppid, "uid": 10001, "user": user,
            "comm": comm or exe.rsplit("/", 1)[-1][:15], "exe": exe, "cmd": cmd,
            "cgroup": "", "unit": unit, "kthread": False, "state": "S", "rss": 1000,
            "cpu_ticks": 10, "start_ticks": 500, "inodes": []}


def _raw(procs, self_pid=10):
    return {"v": 1, "boot_id": BOOT, "self_pid": self_pid, "uptime_s": 6100.0,
            "clk_tck": 100, "procs": procs, "socks": []}


def _box(kind="project", **kw):
    from types import SimpleNamespace
    d = dict(id="p-alpha", kind=kind, project="alpha", host_ip="10.201.10.1",
             guest_ip="10.201.10.2", service_id=None, image=("main", None), runtime="docker")
    d.update(kw)
    return SimpleNamespace(**d)


def _eval(raw, box=None, baseline=None):
    box = box or _box()
    st = procview.BoxState(box.id)
    row, alerts = procview.evaluate(
        box, procview.sanitize_snapshot(raw), st, baseline=baseline or procview.BUILTIN,
        approved=None, host_socks=None, hostnames={}, inbound={}, now=1_800_000_000.0)
    return row, alerts


# --- vendor units -----------------------------------------------------------------------

IMAGE = {"v": 1, "units_enabled": ["ssh.service"],
         "units_vendor": ["apt-listchanges.service", "man-db.service", "fstrim.service",
                          "avahi-daemon.service", "getty@.service", "jarvis-guest.service",
                          "jav3-svc-1.service", "bad unit;rm.service", "motd-news.service"]}


def test_every_vendor_shipped_unit_is_baseline():
    b = procview.parse_baseline(IMAGE)
    assert b.source == "image"
    for exe, unit in [("/usr/bin/python3.13", "apt-listchanges.service"),
                      ("/usr/bin/apt-get", "apt-listchanges.service"),
                      ("/usr/sbin/avahi-daemon", "avahi-daemon.service"),
                      ("/usr/bin/curl", "motd-news.service"),
                      ("/sbin/agetty", "getty@tty1.service")]:         # a template instance
        assert b.matches(exe, unit), (exe, unit)


def test_a_unit_the_image_never_shipped_still_alerts():
    b = procview.parse_baseline(IMAGE)
    assert not b.matches("/usr/bin/python3", "evil.service")
    assert not b.matches("/tmp/x", "fwupd-refresh.service")
    # even listed, the guest server's own unit and service units are never baseline
    assert not b.matches("/usr/bin/python3", "jarvis-guest.service")
    assert not b.matches("/usr/bin/python3", "jav3-svc-1.service")
    assert all("rm" not in u for _, u in b.patterns)


def test_a_new_unit_in_a_running_box_alerts_though_its_neighbours_are_baseline():
    b = procview.parse_baseline(IMAGE)
    kvm = _box(runtime="kvm")
    raw = _raw([
        _p(1, 0, "/usr/lib/systemd/systemd", "/sbin/init", unit="init.scope"),
        _p(10, 1, "/usr/bin/python3", "python3 guest.py", unit="jarvis-guest.service"),
        _p(60, 1, "/usr/bin/apt-get", "apt-get -qq changelog x", unit="apt-listchanges.service"),
        _p(61, 1, "/usr/bin/python3", "python3 -c implant", unit="backdoor.service"),
    ])
    _, alerts = _eval(raw, kvm, b)
    assert [a["detail"]["exe"] for a in alerts] == ["/usr/bin/python3"]
    assert alerts[0]["detail"]["unit"] == "backdoor.service" and "rule" not in alerts[0]


# --- docker's tini ----------------------------------------------------------------------

def test_a_docker_boxs_tini_is_the_os():
    raw = _raw([_p(1, 0, "/usr/bin/tini", "/usr/bin/tini -- python3 -I bootstrap.py"),
                _p(10, 1, "/usr/bin/python3", "python3 -I bootstrap.py")])
    assert _eval(raw)[1] == []
    # a tini that is not PID 1 is no longer "the box's init": it reads as the
    # run_code orphan it is (test_secnotify keeps the unexpected reading with no
    # guest server in the box)
    raw = _raw([_p(1, 0, "/usr/bin/docker-init", "docker-init -- x"),
                _p(10, 1, "/usr/bin/python3", "python3 -I bootstrap.py"),
                _p(57, 1, "/usr/bin/tini", "/usr/bin/tini -- sleep 1")])
    assert [bool(a.get("rule")) for a in _eval(raw)[1]] == [True]


# --- started by the agent -----------------------------------------------------------------

def _docker_box_after_a_screenshot():
    """tini -> guest server; run_code left a node server and a Chromium (with
    its crashpad handler) running, re-parented to tini."""
    return _raw([
        _p(1, 0, "/usr/bin/tini", "/usr/bin/tini -- python3 -I bootstrap.py"),
        _p(10, 1, "/usr/bin/python3", "python3 -I bootstrap.py"),
        _p(50, 1, "/usr/bin/node", "node scripts/serve.mjs"),
        _p(51, 1, "/usr/lib/chromium/chromium", "/usr/lib/chromium/chromium --headless --screenshot"),
        _p(52, 51, "/usr/lib/chromium/chromium", "/usr/lib/chromium/chromium --type=renderer"),
        _p(53, 51, "/usr/lib/chromium/chrome_crashpad_handler",
           "/usr/lib/chromium/chrome_crashpad_handler --monitor-self"),
    ])


def test_what_run_code_left_running_is_recorded_quietly_not_alerted():
    row, alerts = _eval(_docker_box_after_a_screenshot())
    assert alerts and all(a["rule"] and a["kind"] == "unexpected_process" for a in alerts)
    # one per exe/script: node serve.mjs, chromium (two processes, one row), crashpad
    assert sorted(a["detail"]["exe"] for a in alerts) == [
        "/usr/bin/node", "/usr/lib/chromium/chrome_crashpad_handler",
        "/usr/lib/chromium/chromium"]
    assert all("started by the agent" in a["rule"] for a in alerts)
    assert row["totals"]["unexpected"] == 0
    tags = {n["pid"]: n["tag"] for n in _flatten(row["tree"])}
    assert tags[50] == tags[51] == tags[52] == tags[53] == "run_code"
    # and again next cycle: nothing new
    box = _box()
    st = procview.BoxState(box.id)
    snap = procview.sanitize_snapshot(_docker_box_after_a_screenshot())
    kw = dict(baseline=procview.BUILTIN, approved=None, host_socks=None, hostnames={},
              inbound={}, now=1_800_000_000.0)
    assert len(procview.evaluate(box, snap, st, **kw)[1]) == 3
    assert procview.evaluate(box, snap, st, **kw)[1] == []


def _flatten(tree):
    for n in tree:
        yield n
        yield from _flatten(n["children"])


def test_a_process_in_a_unit_the_image_never_shipped_still_alerts_in_a_docker_box():
    raw = _docker_box_after_a_screenshot()
    raw["procs"].append(_p(70, 1, "/tmp/.x/miner", "/tmp/.x/miner", unit="backdoor.service"))
    _, alerts = _eval(raw)
    loud = [a for a in alerts if not a.get("rule")]
    assert [a["detail"]["exe"] for a in loud] == ["/tmp/.x/miner"]


def test_a_vm_process_outside_any_unit_still_alerts():
    """In a VM the guest server has a unit, and a run_code child keeps it. A
    process in no unit at all is not that."""
    raw = _raw([
        _p(1, 0, "/usr/lib/systemd/systemd", "/sbin/init", unit="init.scope"),
        _p(10, 1, "/usr/bin/python3", "python3 guest.py", unit="jarvis-guest.service"),
        _p(80, 1, "/usr/bin/node", "node scripts/serve.mjs", unit=""),
    ])
    _, alerts = _eval(raw, _box(runtime="kvm"))
    assert [(a["detail"]["exe"], bool(a.get("rule"))) for a in alerts] == [("/usr/bin/node", False)]


def test_the_same_orphan_in_a_service_box_still_alerts_critical():
    raw = _raw([_p(1, 0, "/usr/bin/tini", "tini"), _p(10, 1, "/usr/bin/python3", "svcd"),
                _p(50, 1, "/usr/bin/node", "node scripts/serve.mjs")])
    _, alerts = _eval(raw, _box(kind="service"))
    assert [(a["severity"], bool(a.get("rule"))) for a in alerts] == [("critical", False)]


def test_a_guest_cannot_fill_the_log_with_names():
    procs = [_p(1, 0, "/usr/bin/tini", "tini"), _p(10, 1, "/usr/bin/python3", "server")]
    procs += [_p(100 + i, 1, f"/tmp/p{i}", f"/tmp/p{i}") for i in range(80)]
    _, alerts = _eval(_raw(procs))
    assert len(alerts) == procview.MAX_AGENT_RECORDS_PER_BOOT


# --- through the poller into the log --------------------------------------------------------

@pytest.fixture
def reg(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    boxes.registry.reset()
    procview.reset()
    yield
    boxes.registry.reset()
    procview.reset()


async def test_the_poller_files_agent_processes_quietly_and_real_ones_loudly(reg, monkeypatch):
    from backend.db import get_db, init_db
    await init_db()
    security._pings.clear()
    box = boxes.allocate("project", project="alpha")
    monkeypatch.setattr(procview, "_pollable", lambda b: b.id == box.id)

    async def sample(box_list):
        return {}
    monkeypatch.setattr(procview, "sample_host", sample)
    raw = _docker_box_after_a_screenshot()
    raw["procs"].append(_p(70, 1, "/tmp/.x/miner", "/tmp/.x/miner", unit="backdoor.service"))

    async def fake(b):
        return raw
    procview.register_fetcher("project", fake)
    try:
        await procview.poll_once(now=1_800_000_000.0)
        await procview.poll_once(now=1_800_000_005.0)
    finally:
        procview._fetchers.clear()
    db = await get_db()
    try:
        evs = await security.list_events(db)
        waiting = await security.list_events(db, unacknowledged_only=True)
    finally:
        await db.close()
    assert len(evs) == 4                                   # 3 recorded + 1 alert, once each
    assert [e["detail"]["exe"] for e in waiting] == ["/tmp/.x/miner"]
    quiet = [e for e in evs if e["quiet"] == "rule"]
    assert len(quiet) == 3 and all(e["acknowledged"] for e in quiet)
    assert all("started by the agent" in e["rule"] for e in quiet)
    assert all(e["summary"].startswith("Process started by the agent") for e in quiet)
