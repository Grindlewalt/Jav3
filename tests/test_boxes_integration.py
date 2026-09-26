"""Integration seams between the box work packages (WP1-WP8), connected by
the backend integrator: each test drives one hook that used to no-op."""
import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from backend.config import settings
from backend.vm import boxes


@pytest.fixture
def on(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_max_boxes", 8)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 3)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 10**6)
    boxes.registry.reset()
    yield
    boxes.registry.reset()


@pytest.fixture
def docker_on(on, monkeypatch):
    # AF_UNIX paths are short on macOS: vm_dir under /tmp
    d = Path(tempfile.mkdtemp(prefix="j9", dir="/tmp"))
    monkeypatch.setattr(settings, "vm_dir", d)
    monkeypatch.setattr(settings, "docker_enabled", True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


# --- WP8 docker wiring ------------------------------------------------------------

def test_docker_controller_and_transport_are_loaded_lazily(docker_on, monkeypatch):
    """runtime=docker selects WP8's controller and its checked transport,
    without anyone having imported docker_runtime first."""
    monkeypatch.delitem(boxes._DRIVERS, "docker", raising=False)
    monkeypatch.setitem(boxes._TRANSPORTS, "docker", boxes.UnixTransport)
    box = boxes.allocate("project", project="alpha", runtime="docker")
    from backend.vm import docker_runtime, transport_unix
    ctl = boxes.controller(box)
    assert isinstance(ctl, docker_runtime.DockerBox)
    assert isinstance(box.transport, transport_unix.UnixTransport)


def test_docker_box_json_points_at_the_in_container_forwarder(docker_on):
    box = boxes.allocate("project", project="alpha", runtime="docker")
    bj = box.box_json()
    assert bj["runtime"] == "docker"
    assert bj["net"]["proxy"] == f"http://127.0.0.1:{settings.vm_egress_proxy_port}"
    assert bj["net"]["proxy_socket"] == "/run/jav3/proxy.sock"
    assert bj["gateway"] == {"transport": "unix", "path": "/run/jav3/gateway.sock"}


def test_docker_require_settings_are_real_settings():
    from backend.vm import docker_runtime
    assert settings.docker_require_runsc is False
    assert settings.docker_require_userns is False
    assert not hasattr(docker_runtime, "PENDING_SETTINGS")


async def test_docker_box_up_hook_binds_no_host_address(docker_on, monkeypatch):
    from backend.vm import egress_proxy
    monkeypatch.setattr(settings, "vm_egress", True)
    started = []

    async def start_box(box, host=None, port=None):
        started.append(box.id)
    monkeypatch.setattr(egress_proxy.proxy, "start_box", start_box)
    dbox = boxes.allocate("project", project="alpha", runtime="docker")
    kbox = boxes.allocate("project", project="beta")
    await egress_proxy._box_hook("box_up", dbox)
    await egress_proxy._box_hook("box_up", kbox)
    assert started == [kbox.id]


class _W:
    """A minimal StreamWriter for the proxy's handle_conn."""
    def __init__(self):
        self.data, self.closed = b"", False

    def get_extra_info(self, name):
        return ""                      # AF_UNIX peername

    def write(self, b):
        self.data += b

    async def drain(self):
        pass

    def close(self):
        self.closed = True


def _reader(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


async def test_handle_box_conn_attributes_to_the_docker_box(docker_on, monkeypatch):
    from backend.vm import egress_proxy
    monkeypatch.setattr(settings, "vm_egress", True)
    seen = {}

    async def fake_connect(host, port, cr, cw, att=None):
        seen.update(att, host=host)
    monkeypatch.setattr(egress_proxy, "_handle_connect", fake_connect)
    box = boxes.allocate("project", project="alpha", runtime="docker")
    await egress_proxy.handle_box_conn(
        box, _reader(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org\r\n\r\n"), _W())
    assert seen["host"] == "pypi.org"
    assert seen["box_id"] == box.id and seen["project"] == "alpha"
    assert seen["peer_ip"] == "unix" and seen["kind"] == "project"


async def test_handle_box_conn_refuses_non_docker_and_egress_off(docker_on, monkeypatch):
    from backend.vm import egress_proxy
    called = []

    async def fake_connect(*a, **k):
        called.append(a)
    monkeypatch.setattr(egress_proxy, "_handle_connect", fake_connect)
    head = b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n"
    monkeypatch.setattr(settings, "vm_egress", True)
    kbox = boxes.allocate("project", project="beta")
    w = _W()
    await egress_proxy.handle_box_conn(kbox, _reader(head), w)
    await egress_proxy.handle_box_conn(boxes.shared(), _reader(head), _W())
    monkeypatch.setattr(settings, "vm_egress", False)
    dbox = boxes.allocate("project", project="alpha", runtime="docker")
    await egress_proxy.handle_box_conn(dbox, _reader(head), _W())
    assert called == [] and w.closed


async def test_docker_proxy_handler_uses_handle_box_conn(docker_on, monkeypatch):
    from backend.vm import docker_runtime, egress_proxy
    got = []

    async def fake(box, r, w):
        got.append(box.id)
    monkeypatch.setattr(egress_proxy, "handle_box_conn", fake)
    box = boxes.allocate("project", project="alpha", runtime="docker")
    h = docker_runtime.proxy_handler(box)
    assert h is not None                # the direct path, not the TCP splice


async def test_vm_boxes_lists_runtimes(tmp_env, monkeypatch):
    from backend import vm_api
    monkeypatch.setattr(settings, "docker_enabled", False)
    r = await vm_api.list_boxes()
    assert set(r["runtimes"]) == {"kvm", "docker"}
    assert r["runtimes"]["docker"]["available"] is False
    assert "docker_enabled" in r["runtimes"]["docker"]["reason"]


# --- WP4 <-> WP5 baseline, flag-off guest package ---------------------------------

WP5_BASELINE = {"v": 1, "captured_at": "2026-09-26T00:00:00+00:00",
                "dpkg": {"bash": "5.2"}, "pip": {}, "npm": {},
                "units_enabled": ["ssh.service", "jarvis-guest.service",
                                  "jav3-svc-3.service", "getty.target",
                                  "chrony.service", "bad unit;rm.service"],
                "setuid": [], "listening": [],
                "processes": ["python3", "sshd", "chronyd", "ps"]}


def test_wp5_baseline_converts_to_procview_entries():
    from backend.vm import procview
    b = procview.parse_baseline(WP5_BASELINE)
    assert b.source == "image"
    assert b.matches("/usr/sbin/sshd", "ssh.service")
    assert b.matches("/usr/sbin/chronyd", "chrony.service")
    assert b.matches("/usr/lib/systemd/systemd-journald", "systemd-journald.service")
    # never baselined: the guest server's unit, service units, bad names, and
    # bare process names (python3 would otherwise hide in any cgroup)
    assert not b.matches("/usr/bin/python3", "jarvis-guest.service")
    assert not b.matches("/usr/bin/python3", "jav3-svc-3.service")
    assert not b.matches("/usr/bin/python3", "evil.service")
    assert not b.matches("/usr/bin/python3", "session-1.scope")
    assert all("rm" not in u for _, u in b.patterns)
    # WP4's own format is unchanged
    b2 = procview.parse_baseline({"v": 1, "entries": [{"exe": "/x", "unit": "y.service"}]})
    assert b2.matches("/x", "y.service") and not b2.matches("/z", "y.service")


async def test_images_registers_the_baseline_resolver(on):
    import json
    from backend.vm import images, procview
    assert images.baseline_path_for in procview._baseline_resolvers
    settings.vm_dir.mkdir(parents=True, exist_ok=True)
    (settings.vm_dir / "base-v3.qcow2").write_bytes(b"")
    (settings.vm_dir / "base-v3.baseline.json").write_text(json.dumps(WP5_BASELINE))
    box = boxes.allocate("project", project="alpha")
    b = await procview.baseline_for(None, box)
    assert b.source == "image" and b.matches("/usr/sbin/sshd", "ssh.service")


def _pkg_names(flag: bool, monkeypatch) -> set[str]:
    import io
    import tarfile
    from backend.vm import guest_pkg
    monkeypatch.setattr(settings, "vm_boxes_enabled", flag)
    with tarfile.open(fileobj=io.BytesIO(guest_pkg.build_package_tar()), mode="r:gz") as t:
        return set(t.getnames())


def test_flag_off_guest_package_has_no_box_only_files(monkeypatch):
    off = _pkg_names(False, monkeypatch)
    on_ = _pkg_names(True, monkeypatch)
    assert "backend/procwatch.py" not in off
    assert "tools/screenshot/handler.py" not in off
    assert on_ - off == {"backend/procwatch.py", "tools/screenshot/handler.py"}
    assert "backend/server.py" in off and "tools/run_code/handler.py" in off


# --- WP2 <-> WP3 / WP5 / WP8 ------------------------------------------------------

async def test_image_build_policy_is_registry_only_and_fixed(tmp_env):
    from backend import egress, profiles
    from backend.db import get_db, init_db
    from backend.vm import egress_proxy, images
    await init_db()
    assert egress_proxy.IMAGE_BUILD_SLUG == images.BUILD_USER == egress.IMAGE_BUILD
    db = await get_db()
    try:
        for h in ("deb.debian.org", "pypi.org", "files.pythonhosted.org",
                  "registry.npmjs.org", "security.debian.org"):
            assert (await egress.decide(db, egress.IMAGE_BUILD, h))[0] == "allow"
        v, why = await egress.decide(db, egress.IMAGE_BUILD, "evil.example")
        assert v == "deny" and "registr" in why
        assert v == "deny" and why != egress.NOT_LISTED     # never auto mode
        assert await egress.granted_secrets(db, egress.IMAGE_BUILD) == set()
        assert not (await egress.allow_host(db, egress.IMAGE_BUILD, "x.com"))["ok"]
        assert not (await egress.set_lists(db, egress.IMAGE_BUILD, allow=["x.com"]))["ok"]
        assert not (await egress.set_policy(db, egress.IMAGE_BUILD,
                                            mode="denylist"))["ok"]
        assert not (await egress.grant_secret(db, egress.IMAGE_BUILD, "K"))["ok"]
        with pytest.raises(profiles.ProfileError):
            await profiles.assign(db, egress.IMAGE_BUILD, 1)
        pol = await egress.get_policy(db, egress.IMAGE_BUILD)
        assert pol["profile"]["name"] == "Image build"
        assert set(pol["effective_allow"]) == set(egress.IMAGE_BUILD_HOSTS)
    finally:
        await db.close()


async def test_builder_denials_are_never_queued(on, monkeypatch):
    from backend import egress
    from backend.db import get_db, init_db
    from backend.vm import egress_proxy
    await init_db()
    box = boxes.allocate("builder", variant="dev", mem_mb=1024)
    att = egress_proxy.attribute(box, ("10.201.90.2", 40000))
    assert att["project"] == egress.IMAGE_BUILD and att["kind"] == "builder"
    await egress_proxy._record("evil.example", "CONNECT", None, 0, 0, "deny",
                               "image build: package registries only", att)
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) FROM egress_pending") as cur:
            assert (await cur.fetchone())[0] == 0
        async with db.execute("SELECT project_slug, box_id FROM egress_events") as cur:
            assert tuple(await cur.fetchone()) == (egress.IMAGE_BUILD, box.id)
    finally:
        await db.close()


def test_reviewer_never_list_covers_every_box_kind():
    from backend import reviewer
    for k in ("service_requested", "service_approved", "service_revoked",
              "service_rejected", "svc_unreported", "package_requested",
              "package_approved", "package_rejected", "image_variant_built",
              "unexpected_process", "proc_report_mismatch", "profile_changed",
              "profiles_migrated", "docker_weak_isolation",
              "docker_hardening_refused", "docker_socket_refused",
              "persist_imported"):
        assert reviewer.never_auto(k), k
    assert not reviewer.never_auto("egress_auto")


# --- WP4 <-> WP3: svcd answers the process poller ------------------------------------

def _svcd_ps(monkeypatch, snapshot_fn):
    import importlib.util
    import json
    import socket
    import sys
    import types
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("svcd_ps_test", root / "guest/svc/svcd.py")
    svcd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(svcd)
    if snapshot_fn is not None:
        fake = types.ModuleType("backend.procwatch")
        fake.snapshot = snapshot_fn
        monkeypatch.setitem(sys.modules, "backend.procwatch", fake)
        import backend
        monkeypatch.setattr(backend, "procwatch", fake, raising=False)
    a, b = socket.socketpair()
    try:
        a.sendall(b'{"mode":"ps"}\n')
        svcd.handle(b)
        return json.loads(a.makefile("rb").readline())
    finally:
        a.close()
        b.close()


def test_svcd_answers_ps_with_the_run_turn_shape(monkeypatch):
    r = _svcd_ps(monkeypatch, lambda: {"procs": {}, "self_pid": 1})
    assert r == {"type": "ps", "ok": True, "snapshot": {"procs": {}, "self_pid": 1}}


def test_svcd_ps_without_procwatch_says_so(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "backend.procwatch", None)
    r = _svcd_ps(monkeypatch, None)
    assert r["type"] == "ps" and r["ok"] is False and r["error"]


def test_service_unit_names_agree_between_svcd_and_procview():
    import importlib.util
    from backend.vm import procview
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("svcd_units", root / "guest/svc/svcd.py")
    svcd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(svcd)
    # systemd names the cgroup "<unit>.service" for systemd-run --unit <unit>
    m = procview.SVC_UNIT.match(svcd.unit_name(42) + ".service")
    assert m and int(m.group(1)) == 42


# --- WP1 <-> WP3 / WP5 hooks ------------------------------------------------------

from tests.test_boxes_gateway import _rt, _untar  # noqa: E402


async def test_builder_package_is_served_to_the_builder_cid(on):
    from backend.vm import images
    box = boxes.allocate("builder", variant="dev", version="build",
                         mem_mb=settings.vm_builder_box_mem_mb)
    job = images.Job(mode="resolve", variant="dev", items=[{"x": 1}])
    images.builder.jobs[box.id] = job
    try:
        r = await _rt({"op": "get_guest_package"}, peer_cid=box.cid)
        names, bj = _untar(r["tar_b64"])
        assert {"backend/server.py", "backend/job.json", "box.json"} <= set(names)
        assert bj["kind"] == "builder"
        assert "backend/agent/loop.py" not in names   # never the turn package
    finally:
        images.builder.jobs.pop(box.id, None)


async def test_build_report_reaches_the_builder_and_is_gated(on):
    from backend.vm import images
    box = boxes.allocate("builder", variant="dev", version="build",
                         mem_mb=settings.vm_builder_box_mem_mb)
    job = images.Job(mode="resolve", variant="dev")
    images.builder.jobs[box.id] = job
    try:
        r = await _rt({"op": "build_report", "token": job.token, "phase": "log",
                       "line": "hello"}, peer_cid=box.cid)
        assert r == {"type": "build_ack", "ok": True} and job.log == ["hello"]
        r = await _rt({"op": "build_report", "token": "forged", "phase": "log",
                       "line": "x"}, peer_cid=box.cid)
        assert r["error"] == "unknown_job"
        proj = boxes.allocate("project", project="alpha")
        r = await _rt({"op": "build_report", "token": job.token}, peer_cid=proj.cid)
        assert r["error"] == "op_not_allowed"
    finally:
        images.builder.jobs.pop(box.id, None)


async def test_svc_report_reaches_services_and_is_gated(on):
    from backend.db import init_db
    from backend.vm import gateway_server, services
    await init_db()
    assert gateway_server._OP_HANDLERS["svc_report"] is services.on_svc_report
    svc = boxes.allocate("service", project="alpha")
    r = await _rt({"op": "svc_report"}, peer_cid=svc.cid)
    assert r == {"type": "svc_report_ok"}
    proj = boxes.allocate("project", project="alpha")
    r = await _rt({"op": "svc_report"}, peer_cid=proj.cid)
    assert r["error"] == "op_not_allowed"


def test_mem_floor_comes_from_the_variant(on, monkeypatch):
    from backend.vm import images
    assert images.mem_floor in boxes._mem_floors
    assert images.mem_floor("main") is None and images.mem_floor("svc") is None
    assert images.mem_floor("desktop") >= 1280
    monkeypatch.setattr(boxes, "_mem_floors", [lambda v: 2000 if v == "big" else None])
    assert boxes.allocate("project", project="a", variant="big").mem_mb == 2000
    # the desktop setting holds whatever the registered floors say
    assert boxes.allocate("project", project="b", variant="desktop").mem_mb \
        >= settings.vm_desktop_min_mem_mb
    # builders are exempt: they install, they do not run the workload
    assert boxes.allocate("builder", variant="big", mem_mb=1024).mem_mb == 1024


def test_svc_variant_resolves_to_the_base(on):
    settings.vm_dir.mkdir(parents=True, exist_ok=True)
    base = settings.vm_dir / "base-v7.qcow2"
    base.write_bytes(b"")
    box = boxes.allocate("service", project="alpha", variant="svc")
    assert boxes.image_path(box) == base


async def test_service_box_readiness_waits_on_svcd_not_run_turn(on, monkeypatch):
    from backend.vm import lifecycle
    box = boxes.allocate("service", project="alpha")
    ctl = boxes.controller(box)
    dialed = []

    class FakeSock:
        def __init__(self, *a):
            pass

        def connect(self, addr):
            dialed.append(addr)

        def close(self):
            pass

    async def noboot():
        pass
    monkeypatch.setattr(lifecycle, "base_built", lambda: True)
    monkeypatch.setattr(lifecycle.gateway, "enabled", True)
    monkeypatch.setattr(ctl, "boot", noboot)
    monkeypatch.setattr(lifecycle.socket, "socket", FakeSock)
    monkeypatch.setattr(lifecycle.socket, "AF_VSOCK", 40, raising=False)
    await ctl._ensure_ready_locked()
    assert dialed == [(box.cid, boxes.PORT_SVCD)]
