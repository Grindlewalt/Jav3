"""/vms at a glance: box rows (idle timers, doing now, last event), the box
history (boxlog), restart, leftovers (leftovers.py) and the image build log
route. Fake runtimes only: no VM, no container, no docker daemon."""
import json
import os
import time

import httpx
import pytest
from fastapi import FastAPI

from backend import bus, vm_api
from backend.auth import require_user
from backend.config import settings
from backend.db import get_db, init_db
from backend.vm import boxes, boxlog, broker, leftovers, lifecycle


class FakeCtl:
    """GuestVM's interface, in memory."""

    def __init__(self, box=None, running=False):
        self.box = box
        self._run = running
        self.inflight = 0
        self.idle_since = time.monotonic() - 240 if running else None
        self.booted_at = time.monotonic() - 600 if running else None
        self.pid = None
        self.error = None
        self.boots = 0

    def running(self):
        return self._run

    async def acquire(self):
        if not self._run:
            await self.boot()
        self.inflight += 1

    def release(self):
        self.inflight = max(0, self.inflight - 1)
        if self.inflight == 0:
            self.idle_since = time.monotonic()

    async def boot(self):
        self._run, self.boots = True, self.boots + 1
        self.booted_at = self.idle_since = time.monotonic()
        if self.box is not None and not self.box.is_shared:
            await boxes.box_up(self.box)

    async def teardown(self):
        was = self._run
        self._run = False
        self.booted_at = None
        if was and self.box is not None and not self.box.is_shared:
            await boxes.box_down(self.box)


@pytest.fixture
async def env(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_egress", False)
    monkeypatch.setattr(settings, "vm_max_boxes", 4)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 3)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 8000)
    monkeypatch.setattr(settings, "vm_box_idle_stop_seconds", 600)
    monkeypatch.setattr(settings, "vm_idle_scrub_seconds", 900)
    monkeypatch.setattr(settings, "docker_enabled", False)
    monkeypatch.setitem(boxes._DRIVERS, "kvm", lambda b: FakeCtl(b))
    shared = FakeCtl(None, running=True)
    monkeypatch.setattr(lifecycle, "vm", shared)
    boxes.registry.reset()
    boxlog.reset()
    leftovers.reset()
    yield {"shared": shared}
    boxes.registry.reset()
    boxlog.reset()


async def _events(box_id):
    return [(e["event"], e["actor"], e["reason"]) for e in await boxlog.events(box_id)]


# --- rows ------------------------------------------------------------------------

async def test_project_row_says_idle_and_when_it_stops(env):
    b = boxes.allocate("project", project="alpha")
    b.joined.add("beta")
    b.ctl = FakeCtl(b, running=True)
    row = boxes.status_json(b)
    assert row["projects"] == ["alpha", "beta"]
    assert row["activity"] == "idle" and row["state"] == "running"
    assert row["stop_action"] == "stop" and row["stop_after_s"] == 600
    assert 238 <= row["idle_s"] <= 242 and 358 <= row["stops_in_s"] <= 362
    assert abs(row["stops_at"] - (time.time() + row["stops_in_s"])) < 3
    assert abs(row["started_at"] - (time.time() - 600)) < 3
    assert row["ram_cost_mb"] == boxes.ram_cost(b.mem_mb, "kvm")
    b.ctl.inflight = 1
    row = boxes.status_json(b)
    assert row["activity"] == "busy" and row["idle_s"] is None and row["stops_at"] is None


async def test_shared_row_scrub_only_when_on(env, monkeypatch):
    row = boxes.status_json(boxes.shared())
    assert row["stop_action"] == "scrub" and row["stop_after_s"] == 900
    assert row["stops_in_s"] is not None
    monkeypatch.setattr(settings, "vm_idle_scrub_seconds", 0)
    row = boxes.status_json(boxes.shared())
    assert row["stop_action"] is None and row["stops_at"] is None
    assert row["idle_s"] is not None            # still idle, just nothing scheduled


async def test_service_box_has_no_idle_stop_and_stopped_box_no_timer(env):
    s = boxes.allocate("service", project="alpha")
    s.ctl = FakeCtl(s, running=True)
    assert boxes.status_json(s)["stop_action"] is None
    p = boxes.allocate("project", project="alpha")
    row = boxes.status_json(p)
    assert row["activity"] == "stopped" and row["idle_s"] is None and row["started_at"] is None


def test_mins_words():
    assert [boxes._mins(s) for s in (0, 45, 60, 599, 3600, 3900)] == [
        "0s", "45s", "1m", "9m", "1h00m", "1h05m"]


# --- history -------------------------------------------------------------------------

async def test_boot_and_teardown_outside_an_action_are_recorded(env):
    b = boxes.allocate("project", project="alpha")
    await boxes.start(b)
    await boxes.stop(b)
    evs = await _events("p-alpha")
    assert [e[0] for e in evs] == ["stopped", "started"]
    assert evs[1][1] == "app"


async def test_turn_boot_is_attributed_to_the_bound_op(env):
    b = boxes.allocate("project", project="alpha")
    boxes.bind_op("chat:42", b, "alpha")
    await boxes.controller(b).acquire()
    assert (await _events("p-alpha"))[0][:2] == ("started", "turn chat:42")


async def test_operator_destroy_is_one_event_with_actor(env):
    b = boxes.allocate("project", project="alpha")
    await boxes.start(b)
    with boxlog.by("operator grant", "operator destroy"):
        await boxes.destroy(b)
    evs = await _events("p-alpha")
    assert evs[0] == ("destroyed", "operator grant", "operator destroy")
    assert [e[0] for e in evs] == ["destroyed", "started"]      # no separate "stopped"
    assert boxes.get("p-alpha") is None


async def test_idle_reaper_records_idle_stopped_not_destroyed(env):
    b = boxes.allocate("project", project="alpha")
    await boxes.start(b)
    b.ctl.idle_since = time.monotonic() - 700
    await boxes.reap_idle()
    evs = await _events("p-alpha")
    assert evs[0][0] == "idle_stopped" and evs[0][1] == "reaper"
    assert "idle 11m (stops at 10m)" in evs[0][2]
    assert "destroyed" not in [e[0] for e in evs]


async def test_restart_and_a_failing_action(env):
    b = boxes.allocate("project", project="alpha")
    await boxes.start(b)
    with boxlog.by("operator grant"):
        await boxes.restart(b)
    assert (await _events("p-alpha"))[0][:2] == ("restarted", "operator grant")
    assert b.ctl.boots == 2

    async def boom():
        raise RuntimeError("no image")
    b.ctl.boot = boom
    b.ctl._run = False
    with pytest.raises(RuntimeError):
        await boxes.restart(b)
    ev = (await _events("p-alpha"))[0]
    assert ev[0] == "error" and "restarted failed: no image" in ev[2]


async def test_box_event_on_the_bus_and_last_events_survive_memory_loss(env):
    q = bus.subscribe(boxes.BUS_CHAN)
    try:
        b = boxes.allocate("project", project="alpha")
        await boxes.start(b)
        evs = []
        while not q.empty():
            evs.append(q.get_nowait())
        assert {"box_up", "box_event"} <= {e["type"] for e in evs}
        be = next(e for e in evs if e["type"] == "box_event")
        assert be["box_id"] == "p-alpha" and be["event"] == "started"
    finally:
        bus.unsubscribe(boxes.BUS_CHAN, q)
    boxlog.reset()                               # an app restart forgets memory
    last = await boxlog.last_events()
    assert last["p-alpha"]["event"] == "started"


async def test_watch_records_a_crash_once(env):
    class P:
        returncode = 137
    env["shared"]._proc = P()
    await boxlog.watch_all()
    await boxlog.watch_all()
    evs = await _events("shared")
    assert [e[0] for e in evs] == ["crashed"] and "137" in evs[0][2]


async def test_history_is_pruned(env, monkeypatch):
    monkeypatch.setattr(boxlog, "KEEP_ROWS", 3)
    s = boxes.shared()
    for _ in range(6):
        await boxlog.record(s, "started")
    assert len(await boxlog.events("shared")) == 3


# --- doing now ----------------------------------------------------------------------

async def test_now_names_the_turn_its_title_and_tool(env):
    db = await get_db()
    try:
        await db.execute("INSERT INTO conversations (id, summary) VALUES (42, 'fix the build')")
        await db.commit()
    finally:
        await db.close()
    boxlog.start_tracking()
    b = boxes.allocate("project", project="alpha")
    b.ctl = FakeCtl(b, running=True)
    b.ctl.inflight = 1
    env_ = broker.TurnEnvelope(op_id="chat:42", conversation_id=42,
                               active_project="alpha", event_chan="chat:42")
    broker.register_turn(env_)
    boxes.bind_op("chat:42", b, "alpha")
    try:
        bus.publish("chat:42", {"type": "tool", "name": "shell",
                                "args": {"command": "npm test"}})
        r = (await vm_api.list_boxes())
        row = next(x for x in r["boxes"] if x["id"] == "p-alpha")
        assert row["activity"] == "busy"
        (t,) = row["now"]
        assert (t["conversation_id"], t["title"], t["project"]) == (42, "fix the build", "alpha")
        assert t["tool"]["name"] == "shell" and t["tool"]["detail"] == "npm test"
        bus.publish("chat:42", {"type": "tool_result", "name": "shell"})
        row = await vm_api._box_row(b)
        assert row["now"][0]["tool"] is None
        assert r["idle"] == {"project_stop_s": 600, "shared_scrub_s": 900,
                             "reaper_interval_s": settings.vm_reaper_interval_seconds}
    finally:
        broker.release_turn("chat:42")
        boxes.unbind_op("chat:42")


# --- routes -----------------------------------------------------------------------

@pytest.fixture
async def client(env):
    app = FastAPI()
    app.include_router(vm_api.router)
    app.dependency_overrides[require_user] = lambda: {"id": 1, "username": "grant"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        yield c


async def test_routes_restart_events_and_nuke(client, env, monkeypatch):
    b = boxes.allocate("project", project="alpha")
    r = await client.post("/api/vm/boxes/p-alpha/start")
    assert r.status_code == 200 and r.json()["activity"] == "idle"
    r = await client.post("/api/vm/boxes/p-alpha/restart")
    assert r.status_code == 200 and b.ctl.boots == 2
    r = await client.get("/api/vm/boxes/p-alpha/events")
    evs = r.json()["events"]
    assert [e["event"] for e in evs] == ["restarted", "started"]
    assert evs[0]["actor"] == "operator grant" and evs[1]["actor"] == "operator grant"
    assert evs[0]["reason"] == "operator restart"
    r = await client.post("/api/vm/boxes/p-alpha/destroy", json={"confirm": True})
    assert r.status_code == 200
    r = await client.get("/api/vm/boxes/p-alpha/events?limit=1")
    assert [e["event"] for e in r.json()["events"]] == ["destroyed"]
    assert (await client.get("/api/vm/boxes/..%2Fx/events")).status_code in (400, 404)

    class NukeVM(FakeCtl):
        def status(self):
            return {"image_version": "v4", "running": True}

        async def nuke(self):
            async with boxlog.action(boxes.shared(), "nuked", reason="fresh"):
                await self.teardown()
                await self.boot()
    nv = NukeVM(None, running=True)
    monkeypatch.setattr(vm_api, "vm", nv)
    monkeypatch.setattr(lifecycle, "vm", nv)
    assert (await client.post("/api/vm/nuke", json={})).status_code == 400
    r = await client.post("/api/vm/nuke", json={"confirm": True})
    assert r.json()["image_version"] == "v4"
    ev = (await client.get("/api/vm/boxes/shared/events")).json()["events"][0]
    assert (ev["event"], ev["actor"]) == ("nuked", "operator grant")


async def test_real_guestvm_nuke_and_scrub_are_one_event_each(env, monkeypatch):
    g = lifecycle.GuestVM()

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(g, "teardown", noop)
    monkeypatch.setattr(g, "boot", noop)
    with boxlog.by("operator grant"):
        await g.nuke()
    g.running = lambda: True
    g._idle_since = time.monotonic() - 1000
    await g.reap_if_idle()
    evs = await _events("shared")
    assert [e[:2] for e in evs] == [("wiped", "reaper"), ("nuked", "operator grant")]
    assert "idle scrub after 15m" in evs[0][2]


async def test_image_log_route(client, env, monkeypatch):
    assert (await client.get("/api/vm/images/base/log")).status_code == 404
    monkeypatch.setattr(lifecycle.vm, "rebuild_log", {
        "version": "v5", "running": False, "ok": True, "lines": ["a", "b"]}, raising=False)
    monkeypatch.setattr(vm_api, "vm", lifecycle.vm)
    r = (await client.get("/api/vm/images/base/log")).json()
    assert r["lines"] == ["a", "b"] and r["version"] == "v5" and r["source"] == "memory"
    db = await get_db()
    try:
        for v, st, log in ((1, "built", "one\ntwo"), (2, "failed", "x\nERROR: disk full")):
            await db.execute(
                "INSERT INTO image_versions (variant, version, base_version, status, "
                "build_log, built_at) VALUES ('dev', ?, 'v4', ?, ?, datetime('now'))",
                (v, st, log))
        await db.commit()
    finally:
        await db.close()
    r = (await client.get("/api/vm/images/dev/log")).json()
    assert (r["version"], r["ok"], r["error"], r["lines"]) == (2, False, "disk full", ["x"])
    r = (await client.get("/api/vm/images/dev/log?version=1")).json()
    assert r["ok"] and r["lines"] == ["one", "two"]
    assert (await client.get("/api/vm/images/dev/log?version=9")).status_code == 404
    assert (await client.get("/api/vm/images/BAD!/log")).status_code == 400
    from backend.vm import images
    job = images.Job(variant="dev", mode="build") if hasattr(images, "Job") else None
    if job is not None:
        job.version, job.log = 3, ["live 1"]
        monkeypatch.setattr(images.builder, "current", job)
        r = (await client.get("/api/vm/images/dev/log")).json()
        assert r["running"] and r["lines"] == ["live 1"] and r["source"] == "live"


# --- leftovers -----------------------------------------------------------------------

class DockerCLI:
    def __init__(self, sock_root):
        self.calls = []
        self.sock_root = sock_root

    async def run(self, *args, timeout=120):
        self.calls.append(list(args))
        if args[0] == "ps":
            assert "label=jav3.managed=1" in args        # never a looser match
            return 0, "jav3-p-ghost\njav3-p-other\njav3-p-live\n", ""
        if args[0] == "inspect":
            def row(name, src, managed="1"):
                return (f"/{name}\t{managed}\t{name[5:]}\texited\t"
                        + json.dumps([{"Type": "bind", "Source": src}]))
            return 0, "\n".join([
                row("jav3-p-ghost", str(self.sock_root / "12")),
                row("jav3-p-other", "/srv/another-install/vm/sock/12"),
                row("jav3-p-live", str(self.sock_root / "13"))]) + "\n", ""
        return 0, "", ""


def _fake_proc(root, pid, argv, cwd):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    os.symlink(cwd, d / "cwd")


async def test_leftovers_scan_and_clean_only_ours(env, tmp_path, monkeypatch):
    vm_dir = settings.vm_dir
    (vm_dir / "boxes" / "p-ghost").mkdir(parents=True)
    (vm_dir / "boxes" / "p-ghost" / "overlay.qcow2").write_bytes(b"x" * 4096)
    (vm_dir / "boxes" / "p-alpha").mkdir(parents=True)        # registered below
    (vm_dir / "sock" / "12").mkdir(parents=True)
    (vm_dir / "sock" / "12" / "gateway.sock").write_bytes(b"")     # a stale socket left in it
    (vm_dir / "sock" / "14").mkdir(parents=True)                   # empty: not a leftover
    (vm_dir / "overlay.qcow2").write_bytes(b"y")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = tmp_path / "proc"
    _fake_proc(proc, 4001, ["qemu-system-aarch64", "-device", "vhost-vsock-pci,guest-cid=12"],
               str(vm_dir / "boxes" / "p-ghost"))
    _fake_proc(proc, 4002, ["qemu-system-aarch64"], str(elsewhere))   # not ours
    _fake_proc(proc, 4003, ["bash", "run_vm.sh"], str(vm_dir))        # not qemu
    net = tmp_path / "net"
    for n in ("jvtap0", "jvtap12", "eth0", "docker0"):
        (net / n).mkdir(parents=True)
    monkeypatch.setattr(leftovers, "PROC", proc)
    monkeypatch.setattr(leftovers, "SYS_NET", net)
    monkeypatch.setattr(settings, "docker_enabled", True)
    from backend.vm import docker_runtime as dr
    fake = DockerCLI(vm_dir / "sock")
    monkeypatch.setattr(dr, "cli", fake)
    boxes.allocate("project", project="alpha")
    live = boxes.allocate("project", project="live", runtime="docker")
    live.ctl = FakeCtl(live, running=True)
    env["shared"]._run = False                   # the shared overlay is stale

    res = await vm_api.list_leftovers(fresh=True)
    got = {i["id"]: i for i in res["items"]}
    assert set(got) == {"qemu:4001", "container:jav3-p-ghost", "box_dir:p-ghost",
                        "sock_dir:12", "overlay:shared", "tap:jvtap12"}
    assert got["tap:jvtap12"]["cleanable"] is False
    assert got["box_dir:p-ghost"]["bytes"] >= 4096
    assert got["qemu:4001"]["detail"]["cid"] == "12"
    assert "no box p-ghost exists" in got["container:jav3-p-ghost"]["why"]
    assert res["cleanable"] == 5
    assert "6 leftovers" in leftovers.summary_line(res)

    killed = []
    monkeypatch.setattr(leftovers.os, "kill", lambda pid, sig: killed.append(pid))
    with pytest.raises(Exception):
        await vm_api.clean_leftovers(vm_api.CleanBody(confirm=False))
    out = await vm_api.clean_leftovers(vm_api.CleanBody(confirm=True))
    assert sorted(out["removed"]) == sorted(i for i in got if i != "tap:jvtap12")
    assert killed == [4001]
    rms = [c for c in fake.calls if c[0] == "rm"]
    assert rms == [["rm", "--force", "jav3-p-ghost"]]           # never another install's
    assert not (vm_dir / "boxes" / "p-ghost").exists()
    assert (vm_dir / "boxes" / "p-alpha").exists()
    assert not (vm_dir / "overlay.qcow2").exists()
    assert elsewhere.exists()


async def test_an_image_build_qemu_is_never_a_leftover(env, tmp_path, monkeypatch):
    """The base-image build boots its provisioning VM from vm_dir, where the
    shared box's QEMU also runs; a clean during a rebuild once killed it."""
    vm_dir = settings.vm_dir
    proc = tmp_path / "proc"
    _fake_proc(proc, 5001, ["qemu-system-aarch64", "-drive",
                            "file=base-work.qcow2,if=virtio"], str(vm_dir))
    _fake_proc(proc, 5002, ["qemu-system-aarch64", "-netdev", "user,id=n0"], str(vm_dir))
    monkeypatch.setattr(leftovers, "PROC", proc)
    monkeypatch.setattr(leftovers, "SYS_NET", tmp_path / "nonet")
    monkeypatch.setattr(settings, "docker_enabled", False)
    killed = []
    monkeypatch.setattr(leftovers.os, "kill", lambda pid, sig: killed.append(pid))
    res = await vm_api.list_leftovers(fresh=True)
    assert not [i for i in res["items"] if i["type"] == "qemu"]
    await vm_api.clean_leftovers(vm_api.CleanBody(confirm=True))
    assert killed == []


async def test_leftovers_docker_off_never_calls_docker(env, monkeypatch, tmp_path):
    monkeypatch.setattr(leftovers, "PROC", tmp_path / "noproc")
    monkeypatch.setattr(leftovers, "SYS_NET", tmp_path / "nonet")
    from backend.vm import docker_runtime as dr

    class Boom:
        async def run(self, *a, **k):
            raise AssertionError("docker was called")
    monkeypatch.setattr(dr, "cli", Boom())
    res = await leftovers.scan()
    assert res["docker"] == "off" and res["items"] == []
    assert leftovers.summary_line(res) == "[boxes] no leftovers"


async def test_clean_skips_what_is_no_longer_a_leftover(env, monkeypatch, tmp_path):
    monkeypatch.setattr(leftovers, "PROC", tmp_path / "noproc")
    monkeypatch.setattr(leftovers, "SYS_NET", tmp_path / "nonet")
    (settings.vm_dir / "boxes" / "p-beta").mkdir(parents=True)
    boxes.allocate("project", project="beta")        # claimed before the clean
    out = await leftovers.clean(["box_dir:p-beta", "tap:jvtap9"])
    assert out["removed"] == [] and out["skipped"] == ["box_dir:p-beta", "tap:jvtap9"]
    assert (settings.vm_dir / "boxes" / "p-beta").exists()


# --- second box hunt -----------------------------------------------------------------

async def test_a_box_that_never_booted_is_released_after_the_window(env):
    """A start for a project that failed before the guest ever booted left a
    box that never idle-stopped (idle_since stayed None)."""
    b = boxes.allocate("project", project="typo")
    b.allocated_at = time.time() - 30
    await boxes.reap_idle()
    assert boxes.get("p-typo") is b                       # window is 10 minutes
    b.allocated_at = time.time() - 700
    await boxes.reap_idle()
    assert boxes.get("p-typo") is None
    assert boxes.budget()["project_boxes"] == 0


def _fake_stat(root, pid, ppid, utime=0, stime=0, cutime=0, cstime=0, pages=0):
    """/proc/<pid>/stat and statm as the kernel writes them (fields 3.. after
    the comm; a comm with a space and a paren, as real ones can have)."""
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    rest = ["S", str(ppid)] + ["0"] * 9 + [str(utime), str(stime), str(cutime),
                                           str(cstime)] + ["0"] * 5
    (d / "stat").write_text(f"{pid} (py (x)) " + " ".join(rest) + "\n")
    (d / "statm").write_text(f"999 {pages} 10 0 0 0 0\n")


async def test_docker_box_rss_and_cpu_are_the_whole_container(env, monkeypatch, tmp_path):
    """The row measured tini (PID 1): 1.1 MB for a box that uses about 36."""
    root = tmp_path / "proc"
    tck, page = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
    _fake_stat(root, 1092, 1, utime=tck, pages=10)                          # tini
    _fake_stat(root, 1200, 1092, utime=2 * tck, stime=tck, pages=100)       # server
    _fake_stat(root, 1300, 1200, utime=tck, pages=50)                       # its child
    _fake_stat(root, 1400, 1092, cutime=4 * tck, pages=5)                   # reaped work
    _fake_stat(root, 2000, 1, utime=99 * tck, pages=9999)                   # not ours
    monkeypatch.setattr(boxes, "_PROC", str(root))
    st = boxes._proc_stats(1092, tree=True)
    assert st == {"rss_bytes": 165 * page, "cpu_s": 9.0}
    assert boxes._proc_stats(1092) == {"rss_bytes": 10 * page, "cpu_s": 1.0}   # a QEMU: one pid
    assert boxes._proc_stats(31337, tree=True) == {"rss_bytes": None, "cpu_s": None}
    b = boxes.allocate("project", project="alpha")
    b.runtime = "docker"
    b.ctl = FakeCtl(b, running=True)
    b.ctl.pid = 1092
    assert boxes.status_json(b)["rss_bytes"] == 165 * page


async def test_cpu_pct_is_never_negative_after_a_restart(env, monkeypatch, tmp_path):
    root = tmp_path / "proc"
    _fake_stat(root, 500, 1, utime=100000)
    monkeypatch.setattr(boxes, "_PROC", str(root))
    boxes._cpu_prev.clear()
    b = boxes.allocate("project", project="alpha")
    b.ctl = FakeCtl(b, running=True)
    b.ctl.pid = 500
    assert boxes.status_json(b)["cpu_pct"] is None            # first sample
    b.ctl.pid = 501                                           # restarted: fewer ticks
    _fake_stat(root, 501, 1, utime=5)
    assert boxes.status_json(b)["cpu_pct"] is None            # new guest, new count
    _fake_stat(root, 501, 1, utime=205)
    assert boxes.status_json(b)["cpu_pct"] > 0
    b.ctl._run = False
    assert boxes.status_json(b)["cpu_pct"] is None and b.id not in boxes._cpu_prev


async def test_an_idle_shared_vm_is_scrubbed_once_not_every_window(env, monkeypatch):
    """Every window of idleness rebooted the guest again (boot restarted the
    idle clock) and wrote a 'wiped' row each time."""
    g = lifecycle.GuestVM()
    boots = []

    async def fake_boot():
        boots.append(1)
        g._proc = type("P", (), {"returncode": None, "pid": 1})()
        g._idle_since = time.monotonic()
        g._fresh = True                          # what the real boot() ends with

    async def fake_teardown():
        g._proc = None
    monkeypatch.setattr(g, "boot", fake_boot)
    monkeypatch.setattr(g, "teardown", fake_teardown)
    await fake_boot()
    g._fresh = False                             # a turn used it
    g._idle_since = time.monotonic() - 1000
    await g.reap_if_idle()
    assert len(boots) == 2                       # scrubbed: rebooted fresh
    for _ in range(3):
        g._idle_since = time.monotonic() - 1000  # the next windows pass, nobody came
        await g.reap_if_idle()
    assert len(boots) == 2
    assert [e[0] for e in await _events("shared")] == ["wiped"]
    g._proc = type("P", (), {"returncode": None, "pid": 1})()
    g.starting = False

    async def ready():
        return None
    monkeypatch.setattr(g, "_ensure_ready_locked", ready)
    await g.acquire()                            # someone used it: the next idle scrubs again
    g.release()
    g._idle_since = time.monotonic() - 1000
    await g.reap_if_idle()
    assert len(boots) == 3


async def test_history_is_pruned_per_box(env, monkeypatch):
    monkeypatch.setattr(boxlog, "KEEP_PER_BOX", 4)
    s = boxes.shared()
    p = boxes.allocate("project", project="alpha")
    await boxlog.record(p, "started")
    for _ in range(9):
        await boxlog.record(s, "wiped")
    assert len(await boxlog.events("shared")) == 4
    assert [e["event"] for e in await boxlog.events("p-alpha")] == ["started"]


async def test_a_stopped_project_box_says_when_it_will_be_removed(env):
    """A stopped project box vanished from /vms with no warning (idle stop and
    the operator's stop both end in a release)."""
    b = boxes.allocate("project", project="alpha")
    await boxes.start(b)
    await boxes.stop(b)
    b.ctl.idle_since = time.monotonic() - 240
    row = boxes.status_json(b)
    assert row["state"] == "stopped" and row["stops_in_s"] is None
    assert 358 <= row["removed_in_s"] <= 362                 # 600 - 240
    b.ctl.idle_since = time.monotonic() - 900
    assert boxes.status_json(b)["removed_in_s"] == 0
    await boxes.start(b)
    assert boxes.status_json(b)["removed_in_s"] is None      # running: the idle timer instead
    assert boxes.status_json(boxes.shared())["removed_in_s"] is None


async def test_start_for_a_project_that_does_not_exist_is_a_404(client, env):
    """A typo'd start reserved RAM and booted a box for nothing."""
    r = await client.post("/api/vm/boxes/p-no-such-project/start")
    assert r.status_code == 404 and "no project" in r.json()["detail"]
    assert boxes.get("p-no-such-project") is None and boxes.budget()["project_boxes"] == 0
    db = await get_db()
    try:
        await db.execute("INSERT INTO projects (slug, name, path) VALUES ('real', 'real', '/x')")
        await db.commit()
    finally:
        await db.close()
    r = await client.post("/api/vm/boxes/p-real/start")
    assert r.status_code == 200 and r.json()["state"] == "running"


async def test_events_of_an_unknown_box_is_a_404_and_a_gone_boxs_history_reads(client, env):
    assert (await client.get("/api/vm/boxes/nope/events")).status_code == 404
    b = boxes.allocate("project", project="alpha")
    assert (await client.get("/api/vm/boxes/p-alpha/events")).json()["events"] == []
    await client.post("/api/vm/boxes/p-alpha/start")
    await client.post("/api/vm/boxes/p-alpha/destroy", json={"confirm": True})
    assert boxes.get("p-alpha") is None
    evs = (await client.get("/api/vm/boxes/p-alpha/events")).json()["events"]
    assert evs[0]["event"] == "destroyed"
    assert b.id == "p-alpha"


async def test_operator_stop_says_when_it_cut_off_turns(client, env):
    b = boxes.allocate("project", project="alpha")
    await client.post("/api/vm/boxes/p-alpha/start")
    b.ctl.inflight = 2
    await client.post("/api/vm/boxes/p-alpha/stop")
    ev = (await client.get("/api/vm/boxes/p-alpha/events")).json()["events"][0]
    assert ev["event"] == "stopped"
    assert ev["reason"] == "operator stop, cutting off 2 running turns"
    r = await client.post("/api/vm/boxes/shared/destroy", json={"confirm": True})
    assert r.json() == {"ok": True, "removed": False}          # only stopped
    r = await client.post("/api/vm/boxes/p-alpha/destroy", json={"confirm": True})
    assert r.json() == {"ok": True, "removed": True}


async def test_a_dead_docker_daemon_is_not_no_leftovers(env, monkeypatch, tmp_path):
    monkeypatch.setattr(leftovers, "PROC", tmp_path / "noproc")
    monkeypatch.setattr(leftovers, "SYS_NET", tmp_path / "nonet")
    monkeypatch.setattr(settings, "docker_enabled", True)
    from backend.vm import docker_runtime as dr

    class Dead:
        rc = 1

        async def run(self, *a, **k):
            return self.rc, "", "Cannot connect to the Docker daemon at unix:///nonexistent.sock"
    dead = Dead()
    monkeypatch.setattr(dr, "cli", dead)
    for rc in (1, 124):                     # refused, and timed out
        dead.rc = rc
        leftovers.reset()
        res = await leftovers.scan()
        assert res["docker"].startswith("unavailable") and "Cannot connect" in res["docker"]
        assert res["items"] == []
        assert "docker could not be asked" in leftovers.summary_line(res)
    with pytest.raises(dr.DockerError):     # startup's orphan reap says it skipped
        await dr.reap_orphans()


async def test_a_raising_reaper_duty_is_recorded_and_skips_nothing(env, monkeypatch):
    """`except Exception: pass` around the whole tick: one failing duty hid
    itself and skipped project-box reaping and crash detection too."""
    import asyncio
    reaped = []

    class BadVM(FakeCtl):
        async def reap_if_idle(self):
            raise RuntimeError("scrub blew up")

    async def reap_idle():
        reaped.append(1)

    async def scan():
        return {"items": [], "docker": "off"}
    monkeypatch.setattr(lifecycle, "vm", BadVM(None, running=True))
    monkeypatch.setattr(boxes, "reap_idle", reap_idle)
    monkeypatch.setattr(leftovers, "scan", scan)
    monkeypatch.setattr(settings, "vm_reaper_interval_seconds", 0.01)
    lifecycle._reaper_noted.clear()
    task = asyncio.create_task(lifecycle.reaper_loop())
    await asyncio.sleep(0.25)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(reaped) >= 3                                  # the other duties kept running
    evs = await boxlog.events("shared")
    assert [e["event"] for e in evs] == ["error"]            # once, not every tick
    assert "reaper scrub: RuntimeError: scrub blew up" == evs[0]["reason"]
