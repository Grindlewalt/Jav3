"""WP4: process + connection telemetry (Security > Persistent).

Fixture: tests/fixtures/procwatch/turnbox is one project box (p-alpha,
10.201.10.2) as its /proc and `ss -tinpHe` would show it:
  - OS: systemd, kthreads, journald, dbus, cron, the guest server (pid 400)
  - run_code: a nohup'd `python3 -m http.server 8000` (ppid 1, still in
    jarvis-guest.service) and a curl the server spawned
  - unexpected: a "miner" masquerading as a kworker, from a deleted binary, in
    fwupd-refresh.service, with a sh -> sleep chain under it
  - an orphan listening socket (node inspector :9229) no process reports
host_ss.txt is the HOST's own `ss -tinH dst 10.201.0.0/16`, including a proxy
connection from guest port 52000 that nothing in the guest reports.
procs.json holds per-pid files; `_materialise` turns it into a real /proc
tree (symlinks for exe and fd) under tmp_path.
"""
import asyncio
import importlib.util
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend import bus, events_api, procview_api, security
from backend.config import settings
from backend.vm import boxes, procview

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "procwatch" / "turnbox"


def _load_procwatch():
    spec = importlib.util.spec_from_file_location(
        "procwatch_under_test", ROOT / "guest" / "backend" / "procwatch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pw = _load_procwatch()


def _materialise(root: Path) -> Path:
    proc = root / "proc"
    (proc / "net").mkdir(parents=True)
    for name in ("tcp", "tcp6", "udp", "udp6"):
        (proc / "net" / name).write_text((FIX / "net" / name).read_text())
    (proc / "uptime").write_text((FIX / "uptime").read_text())
    (proc / "sys" / "kernel" / "random").mkdir(parents=True)
    (proc / "sys" / "kernel" / "random" / "boot_id").write_text((FIX / "boot_id").read_text())
    for pid, d in json.loads((FIX / "procs.json").read_text()).items():
        pd = proc / pid
        (pd / "fd").mkdir(parents=True)
        (pd / "stat").write_text(d["stat"] + "\n")
        (pd / "status").write_text(f"Name:\tx\nUid:\t{d['uid']}\t{d['uid']}\t{d['uid']}\t{d['uid']}\n")
        (pd / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in d["cmdline"]))
        (pd / "cgroup").write_text(d["cgroup"] + "\n")
        if d["exe"]:
            os.symlink(d["exe"], pd / "exe")
        for fd, target in d["fds"].items():
            os.symlink(target, pd / "fd" / fd)
    return root


@pytest.fixture
def snap_raw(tmp_path):
    root = _materialise(tmp_path / "guest")
    return pw.snapshot(str(root), self_pid=400, ss_text=(FIX / "ss.txt").read_text())


def _box(**kw):
    d = dict(id="p-alpha", kind="project", project="alpha", host_ip="10.201.10.1",
             guest_ip="10.201.10.2", service_id=None, image=("main", None))
    d.update(kw)
    return SimpleNamespace(**d)


def _baseline():
    return procview.parse_baseline(json.loads((FIX / "baseline.json").read_text()))


def _eval(snap, box=None, st=None, *, baseline=None, approved=None, host=None,
          hostnames=None, inbound=None, now=1_800_000_000.0):
    box = box or _box()
    st = st or procview.BoxState(box.id)
    row, alerts = procview.evaluate(
        box, snap, st, baseline=baseline or _baseline(), approved=approved,
        host_socks=host, hostnames=hostnames or {}, inbound=inbound or {}, now=now)
    return row, alerts, st


def _flat(tree):
    for n in tree:
        yield n
        yield from _flat(n["children"])


def _host_socks():
    other = _box(id="p-beta", host_ip="10.201.11.1", guest_ip="10.201.11.2")
    return procview.host_socks_from_ss((FIX / "host_ss.txt").read_text(), [_box(), other])


# --- guest parsing ---------------------------------------------------------------------

def test_parse_stat_handles_parens_and_spaces_in_comm():
    st = pw.parse_stat("900 (kworker/u8:3 (x)) S 1 900 900 0 -1 4194560 1 0 0 0 "
                       "9000 100 0 0 20 0 1 0 5500 1 60000 0")
    assert st["comm"] == "kworker/u8:3 (x)" and st["ppid"] == 1
    assert st["cpu_ticks"] == 9100 and st["start_ticks"] == 5500 and st["rss_pages"] == 60000
    assert pw.parse_stat("garbage") is None and pw.parse_stat("1 (x) S") is None


def test_proc_net_parse():
    tcp = pw.parse_proc_net((FIX / "net" / "tcp").read_text(), "tcp")
    assert tcp[0] == {"proto": "tcp", "laddr": "0.0.0.0", "lport": 8000, "raddr": "0.0.0.0",
                      "rport": 0, "state": "LISTEN", "inode": 20001}
    assert tcp[1]["laddr"] == "10.201.10.2" and tcp[1]["rport"] == 8443
    assert tcp[3]["state"] == "TIME-WAIT" and tcp[3]["inode"] == 0
    tcp6 = pw.parse_proc_net((FIX / "net" / "tcp6").read_text(), "tcp6")
    assert tcp6[0]["laddr"] == "10.201.10.2" and tcp6[0]["lport"] == 43300   # v4-mapped
    assert tcp6[1]["laddr"] == "::" and tcp6[1]["state"] == "LISTEN"
    udp = pw.parse_proc_net((FIX / "net" / "udp").read_text(), "udp")
    assert udp[0]["state"] == "UNCONN" and udp[0]["lport"] == 5353
    assert pw.parse_proc_net("hdr\n  0: zz:zz yy:yy 01 a b c d e f\n", "tcp") == []


def test_parse_ss():
    rows = pw.parse_ss((FIX / "ss.txt").read_text())
    by_ino = {r["inode"]: r for r in rows}
    assert by_ino[20002]["bytes_sent"] == 1520 and by_ino[20002]["bytes_received"] == 48213
    assert by_ino[20002]["pids"] == [845]
    assert by_ino[20010]["laddr"] == "10.201.10.2" and by_ino[20010]["lport"] == 43300
    assert by_ino[20011]["laddr"] == "::" and by_ino[20011]["pids"] == []
    assert by_ino[20001]["bytes_sent"] is None                 # LISTEN: no counters
    assert pw.split_hostport("10.0.0.1%eth0:53") == ("10.0.0.1", 53)
    assert pw.split_hostport("*:*") == ("*", None)


def test_unit_of_and_cgroup():
    assert pw.parse_cgroup("12:pids:/x\n1:name=systemd:/system.slice/a.service\n") == \
        "/system.slice/a.service"
    assert pw.parse_cgroup("0::/system.slice/jav3-svc-7.service\n") == \
        "/system.slice/jav3-svc-7.service"
    assert pw.unit_of("/user.slice/user-1000.slice/session-3.scope") == "session-3.scope"
    assert pw.unit_of("/") == ""


def test_snapshot_from_fixture(snap_raw):
    s = snap_raw
    assert s["v"] == 1 and s["self_pid"] == 400 and s["uptime_s"] == 6100.52
    assert s["boot_id"].startswith("5b0c6a1e")
    procs = {p["pid"]: p for p in s["procs"]}
    assert len(procs) == 13
    assert procs[2]["kthread"] and procs[2]["exe"] == "" and procs[2]["inodes"] == []
    assert procs[812]["unit"] == "jarvis-guest.service"
    assert procs[812]["cmd"] == "python3 -m http.server 8000"
    assert sorted(procs[812]["inodes"]) == [20001, 20005]
    assert procs[900]["exe"] == "/tmp/.cache/.x/miner (deleted)"
    socks = {x["inode"]: x for x in s["socks"]}
    assert socks[20002]["bytes_sent"] == 1520
    assert socks[20003]["bytes_sent"] == 9000000
    assert socks[20050]["bytes_sent"] is None and socks[20050]["proto"] == "udp"


def test_snapshot_without_proc_reports_error_not_crash(tmp_path):
    s = pw.snapshot(str(tmp_path), ss_text="")
    assert s["procs"] == [] and s["errors"] and s["v"] == 1


# --- classification, baseline, tree ---------------------------------------------------

def test_tree_tags_and_descendants(snap_raw):
    snap = procview.sanitize_snapshot(snap_raw)
    row, alerts, _ = _eval(snap)
    roots = {n["pid"]: n for n in row["tree"]}
    assert set(roots) == {812, 845, 900}
    assert roots[812]["tag"] == "run_code" and roots[845]["tag"] == "run_code"
    miner = roots[900]
    assert miner["tag"] == "unexpected" and miner["cmd"] == "[kworker/u8:3]"
    assert [c["pid"] for c in miner["children"]] == [901]
    assert [c["pid"] for c in miner["children"][0]["children"]] == [902]
    shown = {n["pid"] for n in _flat(row["tree"])}
    assert not shown & {1, 2, 3, 15, 210, 330, 400, 950}           # OS + server
    assert miner["started"] == procview._iso(1_800_000_000.0 - (6100.52 - 5500 / 100))
    kinds = [a["kind"] for a in alerts]
    assert kinds.count("unexpected_process") == 3       # 900, 901, 902 (own units)
    assert all(a["severity"] == "warn" for a in alerts)
    assert row["totals"]["unexpected"] == 3 and row["baseline"] == "image"


def test_baseline_member_still_shown_under_unexpected_parent(snap_raw):
    snap = procview.sanitize_snapshot(snap_raw)
    base = procview.parse_baseline({"entries": json.loads(
        (FIX / "baseline.json").read_text())["entries"] + [
        ["/usr/bin/dash", "fwupd-refresh.service"], ["/usr/bin/sleep", "fwupd-refresh.service"]]})
    row, alerts, _ = _eval(snap, baseline=base)
    miner = next(n for n in row["tree"] if n["pid"] == 900)
    assert miner["children"][0]["pid"] == 901
    assert miner["children"][0]["tag"] == "unexpected"            # inherited
    assert [a["detail"]["pid"] for a in alerts if a["kind"] == "unexpected_process"] == [900]


def test_builtin_baseline_when_image_has_none(snap_raw):
    snap = procview.sanitize_snapshot(snap_raw)
    row, _, _ = _eval(snap, baseline=procview.BUILTIN)
    shown = {n["pid"] for n in _flat(row["tree"])}
    assert not shown & {1, 210, 330, 950} and 900 in shown
    assert row["baseline"] == "builtin"


async def test_baseline_resolution(tmp_env):
    from backend.db import get_db, init_db
    await init_db()
    db = await get_db()
    try:
        box = _box(image=("dev", None))
        assert (await procview.baseline_for(db, box)) is procview.BUILTIN
        p = Path(settings.vm_dir) / "b-dev.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"v": 1, "entries": [{"exe": "/x", "unit": "y.service"}]}))
        await db.execute("INSERT INTO image_versions(variant, version, base_version, "
                         "baseline_path, status, active) VALUES ('dev', 2, 'base-v1', ?, "
                         "'built', 1)", (str(p),))
        await db.commit()
        b = await procview.baseline_for(db, box)
        assert b.source == "image" and b.matches("/x", "y.service")
        p.write_text("{not json")
        os.utime(p, (1, 1))
        assert (await procview.baseline_for(db, box)) is procview.BUILTIN
    finally:
        await db.close()


def test_service_box_rules():
    raw = {"boot_id": "b", "self_pid": 100, "uptime_s": 100.0, "clk_tck": 100, "procs": [
        {"pid": 1, "ppid": 0, "exe": "/usr/lib/systemd/systemd", "unit": "init.scope"},
        {"pid": 100, "ppid": 1, "exe": "/usr/bin/python3", "unit": "jav3-svcd.service"},
        {"pid": 150, "ppid": 100, "exe": "/usr/bin/systemd-run", "unit": "jav3-svcd.service"},
        {"pid": 200, "ppid": 1, "exe": "/usr/bin/node", "unit": "jav3-svc-7.service"},
        {"pid": 201, "ppid": 200, "exe": "/usr/bin/node", "unit": "jav3-svc-7.service"},
        {"pid": 300, "ppid": 1, "exe": "/usr/bin/nc", "unit": "jav3-svc-9.service"},
        {"pid": 160, "ppid": 100, "exe": "/usr/bin/curl", "unit": "jav3-svcd.service"},
    ], "socks": []}
    box = _box(id="s-alpha", kind="service", host_ip="10.201.50.1", guest_ip="10.201.50.2")
    base = procview.parse_baseline([["/usr/lib/systemd/systemd", "init.scope"],
                                    ["/usr/bin/python3", "jav3-svcd.service"]])
    row, alerts, _ = _eval(procview.sanitize_snapshot(raw), box, baseline=base, approved={7})
    tags = {n["pid"]: (n["tag"], n["service_id"]) for n in _flat(row["tree"])}
    assert tags[200] == ("service", 7) and tags[201] == ("service", 7)
    assert tags[300] == ("unexpected", 9)          # a svc unit nobody approved here
    assert tags[160][0] == "unexpected" and 150 not in tags and 100 not in tags
    assert {a["detail"]["pid"] for a in alerts} == {300, 160}
    assert all(a["severity"] == "critical" for a in alerts)


# --- join + mismatch ---------------------------------------------------------------------

def test_host_join_verifies_bytes_and_names_hosts(snap_raw):
    snap = procview.sanitize_snapshot(snap_raw)
    host = _host_socks()
    assert set(host) == {"p-alpha", "p-beta"}
    row, _, _ = _eval(snap, host=host["p-alpha"], hostnames={43210: "pypi.org"})
    conns = {(c["lport"], c["state"]): c for n in _flat(row["tree"]) for c in n["conns"]}
    curl = conns[(43210, "ESTAB")]
    assert curl["dir"] == "out" and curl["host"] == "pypi.org"
    assert (curl["guest_bytes_out"], curl["host_bytes_out"]) == (1520, 1520)
    assert (curl["guest_bytes_in"], curl["host_bytes_in"]) == (48213, 48213)
    assert curl["verified"] is True
    assert conns[(51000, "ESTAB")]["verified"] is True          # 10 KB apart: within slack
    assert conns[(43300, "ESTAB")]["verified"] is None          # host has no view of it
    inbound = conns[(8000, "ESTAB")]
    assert inbound["dir"] == "in" and inbound["verified"] is True
    listen = conns[(8000, "LISTEN")]
    assert listen["dir"] == "in" and listen["raddr"] is None
    assert [c["lport"] for c in row["orphan_conns"]] == [9229]
    assert (40000, "TIME-WAIT") not in conns


def test_unreported_connection_alerts_after_two_cycles(snap_raw):
    snap = procview.sanitize_snapshot(snap_raw)
    host = _host_socks()["p-alpha"]
    _, a1, st = _eval(snap, host=host)
    assert not [a for a in a1 if a["kind"] == "proc_report_mismatch"]
    _, a2, st = _eval(snap, st=st, host=host, now=1_800_000_005.0)
    mm = [a for a in a2 if a["kind"] == "proc_report_mismatch"]
    assert len(mm) == 1 and mm[0]["detail"]["reason"] == "unreported_connection"
    assert mm[0]["detail"]["guest"] == "10.201.10.2:52000"
    assert mm[0]["detail"]["host_bytes_out"] == 250000
    _, a3, _ = _eval(snap, st=st, host=host, now=1_800_000_010.0)
    assert not [a for a in a3 if a["kind"] == "proc_report_mismatch"]   # once


def test_blip_that_disappears_does_not_alert(snap_raw):
    snap = procview.sanitize_snapshot(snap_raw)
    host = _host_socks()["p-alpha"]
    _, _, st = _eval(snap, host=host)
    trimmed = {k: v for k, v in host.items() if k[1] != 52000}
    _, a2, st = _eval(snap, st=st, host=trimmed)
    _, a3, _ = _eval(snap, st=st, host=host)
    assert not [a for a in a2 + a3 if a["kind"] == "proc_report_mismatch"]


def test_byte_mismatch(snap_raw):
    snap = procview.sanitize_snapshot(snap_raw)
    host = _host_socks()["p-alpha"]
    key = ("10.201.10.2", 51000, "10.201.10.1", 8443)
    host[key] = dict(host[key], bytes_received=40_000_000)   # guest under-reports
    row, a1, st = _eval(snap, host=host)
    c = next(c for n in _flat(row["tree"]) for c in n["conns"] if c["lport"] == 51000)
    assert c["verified"] is False
    _, a2, _ = _eval(snap, st=st, host=host)
    mm = [a for a in a2 if a["detail"].get("reason") == "byte_mismatch"]
    assert len(mm) == 1 and mm[0]["detail"]["pid"] == 900


def test_no_host_view_means_no_mismatch_claims(snap_raw):
    snap = procview.sanitize_snapshot(snap_raw)
    for _ in range(3):
        row, alerts, st = _eval(snap, host=None)
    assert not [a for a in alerts if a["kind"] == "proc_report_mismatch"]
    assert all(c["verified"] is None for n in _flat(row["tree"]) for c in n["conns"])


def test_inbound_relay_counters_on_listener(snap_raw):
    snap = procview.sanitize_snapshot(snap_raw)
    row, _, _ = _eval(snap, inbound={8000: {"bytes_in": 410, "bytes_out": 20480}})
    listen = next(c for n in _flat(row["tree"]) for c in n["conns"]
                  if c["state"] == "LISTEN" and c["lport"] == 8000)
    assert (listen["host_bytes_in"], listen["host_bytes_out"]) == (410, 20480)


def test_cpu_pct_across_cycles(snap_raw):
    snap = procview.sanitize_snapshot(snap_raw)
    _, _, st = _eval(snap)
    snap2 = procview.sanitize_snapshot(dict(snap_raw, uptime_s=6110.52, procs=[
        dict(p, cpu_ticks=p["cpu_ticks"] + 500) if p["pid"] == 900 else p
        for p in snap_raw["procs"]]))
    row, _, _ = _eval(snap2, st=st)
    miner = next(n for n in row["tree"] if n["pid"] == 900)
    assert miner["cpu_pct"] == 50.0


# --- hostile guest ---------------------------------------------------------------------

def test_sanitize_rejects_garbage():
    for bad in (None, [], "x", {"procs": "x", "socks": []}, {"procs": []}):
        with pytest.raises(ValueError):
            procview.sanitize_snapshot(bad)


def test_sanitize_caps_and_cleans():
    evil = "\u202eexe\x1b[31m" + "A" * 100000
    many = list(range(1, 100000))                  # one shared list, not 12000 copies
    raw = {"boot_id": {"x": 1}, "self_pid": "400", "uptime_s": float("inf"), "clk_tck": 0,
           "procs": [{"pid": i, "ppid": 1, "exe": evil, "cmd": evil, "user": ["root"],
                      "unit": evil, "rss": -5, "cpu_ticks": 2 ** 80,
                      "inodes": many}
                     for i in range(2, 12000)] + ["x", 5, {"pid": True}, {"pid": -1}],
           "socks": [{"proto": "tcp", "laddr": evil, "lport": 70000, "raddr": 1,
                      "rport": "x", "state": "ESTAB", "inode": "12"}] * 9000 +
                    [{"proto": "sctp"}]}
    s = procview.sanitize_snapshot(raw)
    assert len(s["procs"]) == procview.MAX_PROCS and s["truncated"] is True
    p = s["procs"][2]
    assert len(p["exe"]) <= 256 and "\u202e" not in p["exe"] and "\x1b" not in p["exe"]
    assert len(p["cmd"]) <= 512 and p["user"] == "" and p["rss"] is None
    assert p["cpu_ticks"] is None and len(p["inodes"]) == procview.MAX_INODES_PER_PROC
    assert len(s["socks"]) == procview.MAX_SOCKS
    assert s["socks"][0]["lport"] is None and s["socks"][0]["inode"] == 0
    assert s["self_pid"] is None and s["uptime_s"] is None and s["clk_tck"] == 100
    row, alerts, _ = _eval(s, baseline=procview.BUILTIN)
    assert len(alerts) <= procview.MAX_ALERTS_PER_BOX_HOUR
    json.dumps(row)


def test_ppid_cycles_and_deep_chains_terminate():
    procs = [{"pid": 10, "ppid": 11}, {"pid": 11, "ppid": 10}, {"pid": 12, "ppid": 12}]
    procs += [{"pid": 100 + i, "ppid": 99 + i} for i in range(2000)]       # 99 missing
    for p in procs:
        p.update(exe="/x", unit="evil.service")
    s = procview.sanitize_snapshot({"procs": procs, "socks": []})
    row, _, _ = _eval(s, baseline=procview.BUILTIN)
    pids = [n["pid"] for n in _flat(row["tree"])]
    assert sorted(pids) == sorted(p["pid"] for p in procs)            # each exactly once


def test_alert_rate_cap_across_cycles():
    st = procview.BoxState("p-alpha")
    total = 0
    for cycle in range(5):
        procs = [{"pid": 1000 + cycle * 100 + i, "ppid": 1, "exe": f"/bad/{cycle}-{i}",
                  "unit": "x.service"} for i in range(20)]
        s = procview.sanitize_snapshot({"procs": procs, "socks": [], "boot_id": "b"})
        _, alerts, st = _eval(s, st=st, baseline=procview.BUILTIN, now=1e9 + cycle)
        total += len(alerts)
    assert total == procview.MAX_ALERTS_PER_BOX_HOUR


async def test_rpc_reply_is_bounded(monkeypatch):
    a, b = socket.socketpair()
    a.setblocking(False)

    class T:
        async def connect(self, port):
            return a

    box = SimpleNamespace(kind="project", transport=T())
    monkeypatch.setattr(procview, "MAX_REPLY_BYTES", 100_000)
    loop = asyncio.get_running_loop()

    def flood():
        b.recv(100)
        try:
            b.sendall(b"x" * 300_000)
        except OSError:
            pass
    fut = loop.run_in_executor(None, flood)
    with pytest.raises(ValueError, match="too large"):
        await procview.rpc_ps(box)
    b.close()
    await fut


async def test_rpc_happy_path_and_refusal():
    for reply, ok in ((b'{"type":"ps","ok":true,"snapshot":{"procs":[],"socks":[]}}\n', True),
                      (b'{"type":"ps","ok":false,"error":"no procwatch"}\n', False)):
        a, b = socket.socketpair()
        a.setblocking(False)
        b.sendall(reply)

        class T:
            async def connect(self, port, _a=a):
                return _a
        box = SimpleNamespace(kind="service", transport=T())
        if ok:
            assert await procview.rpc_ps(box) == {"procs": [], "socks": []}
        else:
            with pytest.raises(ValueError, match="no procwatch"):
                await procview.rpc_ps(box)
        assert b.recv(100) == b'{"mode":"ps"}\n'
        b.close()


# --- poller, API, SSE ---------------------------------------------------------------------

@pytest.fixture
def reg(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    boxes.registry.reset()
    procview.reset()
    yield
    boxes.registry.reset()
    procview.reset()


async def test_poll_once_end_to_end(reg, monkeypatch, snap_raw):
    from backend.db import get_db, init_db
    await init_db()
    box = boxes.allocate("project", project="alpha")
    hung = boxes.allocate("service", project="alpha")
    monkeypatch.setattr(procview, "_pollable", lambda b: b.id in (box.id, hung.id))
    monkeypatch.setattr(procview, "FETCH_TIMEOUT_S", 0.2)

    async def fake(b):
        return snap_raw

    async def never(b):
        await asyncio.sleep(10)
    procview.register_fetcher("project", fake)
    procview.register_fetcher("service", never)
    host = _host_socks()

    async def sample(box_list):
        return {"p-alpha": host["p-alpha"]}
    monkeypatch.setattr(procview, "sample_host", sample)
    q = bus.subscribe(procview.PROCS_CHAN)
    try:
        await procview.poll_once(now=1_800_000_000.0)
        rows = await procview.poll_once(now=1_800_000_005.0)
    finally:
        bus.unsubscribe(procview.PROCS_CHAN, q)
        procview._fetchers.clear()
    by_id = {r["box_id"]: r for r in rows}
    assert by_id["p-alpha"]["stale"] is False and by_id["p-alpha"]["tree"]
    assert by_id[hung.id]["stale"] is True and "TimeoutError" in by_id[hung.id]["error"]
    ev = q.get_nowait()
    assert ev["type"] == "box_procs" and ev["box"]["box_id"] in ("p-alpha", hung.id)
    db = await get_db()
    try:
        evs = await security.list_events(db)
    finally:
        await db.close()
    kinds = sorted(e["kind"] for e in evs)
    assert kinds.count("unexpected_process") == 3
    assert kinds.count("proc_report_mismatch") == 1
    assert all(e["project_slug"] == "alpha" for e in evs)


def _keys_match_contract(row):
    assert set(row) >= {"box_id", "kind", "project", "reported_at", "stale", "tree"}
    for n in _flat(row["tree"]):
        assert set(n) == {"pid", "ppid", "user", "exe", "cmd", "unit", "service_id", "tag",
                          "rss", "cpu_pct", "started", "conns", "children"}
        assert n["tag"] in ("service", "unexpected", "run_code")
        for c in n["conns"]:
            assert set(c) == {"proto", "dir", "laddr", "lport", "raddr", "rport", "host",
                              "state", "guest_bytes_out", "guest_bytes_in",
                              "host_bytes_out", "host_bytes_in", "verified"}
            assert c["dir"] in ("in", "out")


async def test_api_shape(reg, snap_raw, monkeypatch):
    monkeypatch.setattr(procview, "ensure_started", lambda: None)
    box = boxes.allocate("project", project="alpha")
    st = procview._state.setdefault(box.id, procview.BoxState(box.id))
    import time
    now = time.time()
    row, _, _ = _eval(procview.sanitize_snapshot(snap_raw), box, st,
                      host=_host_socks()["p-alpha"], now=now)
    st.row, st.reported_at = row, now
    out = await procview_api.processes()
    assert out["enabled"] is True
    by_id = {r["box_id"]: r for r in out["boxes"]}
    assert set(by_id) == {"shared", "p-alpha"}
    assert by_id["shared"]["stale"] is True and by_id["shared"]["tree"] == []
    _keys_match_contract(by_id["p-alpha"])
    json.dumps(out)
    one = await procview_api.processes(box="p-alpha")
    assert [r["box_id"] for r in one["boxes"]] == ["p-alpha"]
    with pytest.raises(HTTPException) as e:
        await procview_api.processes(box="p-nope")
    assert e.value.status_code == 404
    st.reported_at = now - 60                                        # stale data
    assert (await procview_api.processes(box="p-alpha"))["boxes"][0]["stale"] is True


async def test_api_flag_off(tmp_env):
    assert await procview_api.processes() == {"enabled": False, "boxes": []}


def test_big_rows_are_announced_not_pushed():
    row = {"box_id": "p-x", "tree": [{"cmd": "x" * 1000}] * 400}
    assert procview.event_for(row) == {"type": "box_procs_changed", "box_id": "p-x"}
    assert procview.event_for({"box_id": "p-x", "tree": []})["type"] == "box_procs"


async def test_procs_topic_on_the_one_event_stream(tmp_env):
    from starlette.requests import Request
    from backend.auth import COOKIE_NAME, make_token
    token = make_token(1, "operator")
    req = Request({"type": "http", "method": "GET", "path": "/api/events",
                   "headers": [(b"cookie", f"{COOKIE_NAME}={token}".encode())],
                   "query_string": b""})
    assert "procs" in events_api.TOPICS
    resp = await events_api.events(req, topics="procs")
    it = resp.body_iterator

    async def nxt():
        while True:
            chunk = await asyncio.wait_for(it.__anext__(), 2)
            if not chunk.startswith(":"):
                return json.loads(chunk[6:])
    try:
        assert await nxt() == {"topic": "procs", "event": {"type": "stream_open",
                                                            "channel": "procs"}}
        bus.publish(procview.PROCS_CHAN, {"type": "box_gone", "box_id": "p-a"})
        assert await nxt() == {"topic": "procs",
                               "event": {"type": "box_gone", "box_id": "p-a"}}
    finally:
        await it.aclose()
    assert bus.subscriber_count(procview.PROCS_CHAN) == 0


async def test_box_down_hook_drops_state_and_never_raises(reg):
    procview._state["p-alpha"] = procview.BoxState("p-alpha")
    await procview._box_hook("box_down", _box())
    assert "p-alpha" not in procview._state
    await procview._box_hook("box_up", None)      # hostile/odd input: no raise
