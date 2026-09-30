"""WP8: the hardened docker box runtime, with a fake docker CLI.

Covers the run spec (every hardening flag present, nothing forbidden), the
network isolation spec, daemon-prerequisite refusal, the unix transport over
real AF_UNIX sockets (both directions, symlink + peer checks), the lifecycle
state machine, and the recipe -> Dockerfile adapter. No docker needed."""
import asyncio
import importlib.util
import json
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from backend.config import settings
from backend.vm import boxes, docker_recipe, docker_runtime as dr, transport_unix as tu

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


boxinfo = _load("guest_boxinfo_under_test", "guest/backend/boxinfo.py")


class guest_unix:
    """The guest's side, through WP1's boxinfo (the only guest module that
    knows the transport) with a docker box.json."""

    @staticmethod
    def listen(port, path):
        boxinfo._cache = {"runtime": "docker",
                          "listen": {"runturn": {"transport": "unix", "path": path}}}
        return boxinfo.listen("runturn", port)

    @staticmethod
    def request(obj, path):
        boxinfo._cache = {"runtime": "docker",
                          "gateway": {"transport": "unix", "path": path}}
        s = boxinfo.gateway_connect()
        try:
            s.sendall((json.dumps(obj) + "\n").encode())
            return json.loads(s.makefile("rb").readline())
        finally:
            s.close()
bootstrap = _load("docker_bootstrap_under_test", "vm/docker/bootstrap.py")

HARDENED_INFO = {"ServerVersion": "27.3.1", "CgroupVersion": "2",
                 "SecurityOptions": ["name=seccomp,profile=builtin", "name=rootless",
                                     "name=cgroupns"],
                 "Runtimes": {"runc": {}, "io.containerd.runc.v2": {}}}


@pytest.fixture
def short_dir(monkeypatch):
    # AF_UNIX paths are capped at ~104 bytes on macOS; pytest's tmp_path is too
    # long. vm_dir points here too: docker sockets live in <vm_dir>/sock/<cid>.
    d = Path(tempfile.mkdtemp(prefix="j8", dir="/tmp"))
    monkeypatch.setattr(settings, "vm_dir", d)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def dsettings(monkeypatch, short_dir):
    monkeypatch.setattr(settings, "vm_dir", short_dir)
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "docker_oci_runtime", "")
    monkeypatch.setattr(settings, "vm_boot_timeout_seconds", 3)
    for k in ("docker_require_runsc", "docker_require_userns"):
        set_req(monkeypatch, k, False)


def set_req(monkeypatch, name, value):
    """docker_require_* are config.py settings."""
    monkeypatch.setattr(settings, name, value)


def make_box(d: Path, kind="project", bid="p-alpha", project="alpha") -> boxes.Box:
    net = boxes.addressing(10)
    net["tap"] = "jvbr10"
    return boxes.Box(id=bid, kind=kind, project=project, cid=10,
                     image=("main", None), mem_mb=512, cpus=1, dir=d / bid,
                     runtime="docker", **net)


def iso(**kw):
    base = dict(oci_runtime=None, userns="rootless", warnings=[], apparmor=False)
    base.update(kw)
    return dr.Isolation(**base)


def flag_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


# --- run spec -----------------------------------------------------------------

def test_transport_is_the_hardened_unix_one(short_dir):
    box = make_box(short_dir)
    t = box.transport
    assert isinstance(t, tu.UnixTransport)
    bj = box.box_json()
    assert bj["gateway"] == {"transport": "unix", "path": "/run/jav3/gateway.sock"}
    assert bj["listen"]["runturn"] == {"transport": "unix",
                                       "path": "/run/jav3/5556.sock"}


def test_run_spec_has_every_hardening_flag(short_dir, dsettings):
    box = make_box(short_dir)
    argv = dr.run_spec(box, iso())
    dr.validate_spec(argv, box)
    assert flag_values(argv, "--network") == ["none"]
    assert flag_values(argv, "--cap-drop") == ["ALL"]
    assert "no-new-privileges=true" in flag_values(argv, "--security-opt")
    assert "--read-only" in argv
    assert flag_values(argv, "--user") == ["10001:10001"]
    assert flag_values(argv, "--pids-limit") == [str(settings.docker_box_pids)]
    assert flag_values(argv, "--memory") == ["512m"]
    assert flag_values(argv, "--memory-swap") == ["512m"]      # no swap
    assert flag_values(argv, "--cpus") == [str(settings.docker_box_cpus)]
    tmpfs = {t.split(":")[0]: t for t in flag_values(argv, "--tmpfs")}
    assert f"size={settings.docker_tmpfs_mb}m" in tmpfs["/tmp"]
    for d in ("/tmp", "/run"):
        assert {"noexec", "nosuid", "nodev"} <= set(tmpfs[d].split(":")[1].split(","))
    assert all("size=" in t for t in tmpfs.values())
    assert flag_values(argv, "--ipc") == ["private"]
    assert flag_values(argv, "--cgroupns") == ["private"]
    assert flag_values(argv, "--restart") == ["no"]
    # default seccomp: never unconfined, and no custom profile that could be
    assert not any("seccomp" in s for s in flag_values(argv, "--security-opt"))
    assert "--runtime" not in argv
    assert argv[-1] == settings.docker_image_turn


def test_run_spec_mounts_only_the_socket_dirs(short_dir, dsettings):
    box = make_box(short_dir)
    argv = dr.run_spec(box, iso())
    mounts = flag_values(argv, "--mount")
    t = box.transport
    assert mounts == [f"type=bind,src={t.host_dir},dst=/run/jav3"]
    assert t.host_dir == settings.vm_dir / "sock" / "10"
    joined = " ".join(argv)
    assert "docker.sock" not in joined and "-v" not in argv and "--volume" not in argv
    assert "--privileged" not in argv and "--cap-add" not in argv


def test_service_box_gets_its_srv_volume(short_dir, dsettings):
    box = make_box(short_dir, kind="service", bid="s-alpha")
    argv = dr.run_spec(box, iso())
    dr.validate_spec(argv, box)
    assert "type=volume,src=jav3-srv-s-alpha,dst=/srv" in flag_values(argv, "--mount")
    assert argv[-1] == settings.docker_image_svc


def test_runsc_and_apparmor_when_available(short_dir, dsettings):
    box = make_box(short_dir)
    argv = dr.run_spec(box, iso(oci_runtime="runsc", apparmor=True))
    dr.validate_spec(argv, box)
    assert flag_values(argv, "--runtime") == ["runsc"]
    assert "apparmor=docker-default" in flag_values(argv, "--security-opt")


def test_network_isolation_spec(short_dir, dsettings):
    """No interface but lo, no ports, no DNS/hosts injection, no aliases: the
    only reachable endpoints are the host sockets in the read-only mount."""
    box = make_box(short_dir)
    argv = dr.run_spec(box, iso())
    assert flag_values(argv, "--network") == ["none"]
    for f in ("-p", "--publish", "-P", "--publish-all", "--dns", "--add-host",
              "--network-alias", "--link", "--expose"):
        assert f not in argv
    # the host end the guest can reach: exactly two sockets, read-only dir
    t = box.transport
    assert t.gateway_endpoint()["path"] == "/run/jav3/gateway.sock"
    assert t.proxy_endpoint() == {"transport": "unix",
                                  "path": "/run/jav3/proxy.sock",
                                  "url": "http://127.0.0.1:8443"}


@pytest.mark.parametrize("mutate", [
    lambda a: a + ["--privileged"],
    lambda a: a + ["--cap-add", "NET_ADMIN"],
    lambda a: a + ["-p", "8080:80"],
    lambda a: a + ["--publish=8080:80"],
    lambda a: a + ["-v", "/var/run/docker.sock:/var/run/docker.sock"],
    lambda a: a + ["--mount", "type=bind,src=/var/run/docker.sock,dst=/d"],
    lambda a: a + ["--mount", "type=bind,src=/home,dst=/h"],
    lambda a: a + ["--security-opt", "seccomp=unconfined"],
    lambda a: a + ["--security-opt", "apparmor=unconfined"],
    lambda a: a + ["--pid", "host"],
    lambda a: a + ["--userns", "host"],
    lambda a: [x if x != "none" else "bridge" for x in a],
    lambda a: [x for x in a if x != "--read-only"],
    lambda a: _drop_flag(a, "--cap-drop"),
    lambda a: _drop_flag(a, "--pids-limit"),
    lambda a: _drop_flag(a, "--memory"),
    lambda a: [x.replace("no-new-privileges=true", "no-new-privileges=false") for x in a],
    lambda a: [x.replace(",noexec", "") if x.startswith("/tmp:") else x for x in a],
    lambda a: [x.replace(f"size={settings.docker_tmpfs_mb}m,", "")
               if x.startswith("/tmp:") else x for x in a],
])
def test_validate_spec_refuses_weakened_specs(short_dir, dsettings, mutate):
    box = make_box(short_dir)
    argv = mutate(dr.run_spec(box, iso()))
    with pytest.raises(dr.DockerHardeningError):
        dr.validate_spec(argv, box)


def _drop_flag(argv, flag):
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == flag:
            skip = True
            continue
        out.append(a)
    return out


# --- daemon prerequisites -------------------------------------------------------

def test_daemon_info_parse():
    info = dr.DaemonInfo.parse({**HARDENED_INFO, "Runtimes": {"runc": {}, "runsc": {}},
                                "SecurityOptions": ["name=apparmor",
                                                    "name=seccomp,profile=default",
                                                    "name=userns"]})
    assert info.userns and not info.rootless and info.apparmor
    assert info.seccomp and info.seccomp_profile == "default"
    assert "runsc" in info.runtimes


def test_no_seccomp_is_always_refused(dsettings):
    info = dr.DaemonInfo.parse({**HARDENED_INFO, "SecurityOptions": ["name=rootless"]})
    with pytest.raises(dr.DockerHardeningError, match="seccomp"):
        dr.plan_isolation(info)


def test_weak_userns_warns_by_default(dsettings):
    info = dr.DaemonInfo.parse({**HARDENED_INFO,
                                "SecurityOptions": ["name=seccomp,profile=builtin"]})
    plan = dr.plan_isolation(info)
    assert plan.weak and plan.userns == "none"
    assert any("neither rootless nor userns" in w for w in plan.warnings)


def test_require_userns_refuses(dsettings, monkeypatch):
    set_req(monkeypatch, "docker_require_userns", True)
    info = dr.DaemonInfo.parse({**HARDENED_INFO,
                                "SecurityOptions": ["name=seccomp,profile=builtin"]})
    with pytest.raises(dr.DockerHardeningError, match="neither rootless"):
        dr.plan_isolation(info)
    ok = dr.plan_isolation(dr.DaemonInfo.parse(HARDENED_INFO))   # rootless passes
    assert ok.userns == "rootless" and not ok.weak


@pytest.mark.parametrize("how", ["setting", "oci"])
def test_require_runsc_refuses_without_it(dsettings, monkeypatch, how):
    if how == "setting":
        set_req(monkeypatch, "docker_require_runsc", True)
    else:
        monkeypatch.setattr(settings, "docker_oci_runtime", "runsc")
    with pytest.raises(dr.DockerHardeningError, match="runsc"):
        dr.plan_isolation(dr.DaemonInfo.parse(HARDENED_INFO))


def test_runsc_auto_detected_and_runc_override(dsettings, monkeypatch):
    info = dr.DaemonInfo.parse({**HARDENED_INFO, "Runtimes": {"runc": {}, "runsc": {}}})
    assert dr.plan_isolation(info).oci_runtime == "runsc"
    monkeypatch.setattr(settings, "docker_oci_runtime", "runc")
    assert dr.plan_isolation(info).oci_runtime is None


def test_guest_uid_mapping(monkeypatch, short_dir):
    sub = short_dir / "subuid"
    sub.write_text("someone:100000:65536\ndockremap:231072:65536\n")
    monkeypatch.setattr(dr, "SUBUID", sub)
    import getpass
    monkeypatch.setattr(getpass, "getuser", lambda: "someone")
    assert dr.guest_host_uid(iso(userns="rootless")) == 100000 + 10001 - 1
    assert dr.guest_host_uid(iso(userns="userns-remap")) == 231072 + 10001
    assert dr.guest_host_uid(iso(userns="none")) == 10001


# --- the unix transport over real sockets ------------------------------------------

async def test_gateway_protocol_over_socketpair():
    """The unmodified gateway handle_conn speaks to the guest client over a
    plain AF_UNIX stream: the transport changes nothing above the socket."""
    from backend.vm import gateway_server
    loop = asyncio.get_running_loop()
    host, guest = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    host.setblocking(False)
    task = asyncio.create_task(gateway_server.handle_conn(loop, host))
    guest.setblocking(False)
    await loop.sock_sendall(guest, b'{"op":"ping"}\n{"op":"nope"}\n')
    buf = b""
    while buf.count(b"\n") < 2:
        buf += await loop.sock_recv(guest, 4096)
    a, b = [json.loads(x) for x in buf.split(b"\n")[:2]]
    assert a == {"type": "pong"} and b["error"] == "unknown_op"
    guest.close()
    await asyncio.wait_for(task, 2)


async def test_guest_to_host_listener_round_trip(short_dir):
    """guest unixsock.request -> host UnixListener bound to one box."""
    seen = []

    async def handler(loop, conn):
        data = b""
        while not data.endswith(b"\n"):
            data += await loop.sock_recv(conn, 4096)
        seen.append(json.loads(data))
        await loop.sock_sendall(conn, b'{"type":"pong","box":"p-alpha"}\n')
        conn.close()

    path = short_dir / "gateway.sock"
    lst = tu.UnixListener(path, handler)
    await lst.start()
    try:
        reply = await asyncio.get_running_loop().run_in_executor(
            None, lambda: guest_unix.request({"op": "ping"}, str(path)))
        assert reply == {"type": "pong", "box": "p-alpha"}
        assert seen == [{"op": "ping"}] and lst.accepted == 1
        assert oct(os.stat(path).st_mode & 0o777) == oct(0o666)
    finally:
        await lst.stop()
    assert not path.exists()


async def test_host_to_guest_run_turn_round_trip(short_dir):
    """host UnixTransport.connect(5556) -> guest unixsock.listen(5556), NDJSON."""
    box = make_box(short_dir)
    t = box.transport
    t.host_dir.mkdir(parents=True)
    srv = guest_unix.listen(5556, str(t.host_path(5556)))
    loop = asyncio.get_running_loop()

    async def guest_side():
        conn, _ = await loop.sock_accept(srv)
        conn.setblocking(False)
        data = b""
        while not data.endswith(b"\n"):
            data += await loop.sock_recv(conn, 4096)
        spec = json.loads(data)
        for ev in ({"type": "token", "content": "hi"},
                   {"type": "final", "content": spec["q"]}):
            await loop.sock_sendall(conn, (json.dumps(ev) + "\n").encode())
        conn.close()

    g = asyncio.create_task(guest_side())
    s = await t.connect(boxes.PORT_RUNTURN)
    await loop.sock_sendall(s, b'{"q":"PONG"}\n')
    buf = b""
    while True:
        chunk = await loop.sock_recv(s, 4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    srv.close()
    await g
    evs = [json.loads(x) for x in buf.splitlines()]
    assert evs[-1] == {"type": "final", "content": "PONG"}


async def test_connect_refuses_a_symlinked_guest_socket(short_dir):
    """A guest that replaces 5556.sock with a symlink to some host socket
    (say the docker socket) gets refused before the host connects."""
    box = make_box(short_dir)
    t = box.transport
    t.host_dir.mkdir(parents=True)
    victim = short_dir / "victim.sock"
    v = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    v.bind(str(victim))
    v.listen(1)
    os.symlink(victim, t.host_path(5556))
    with pytest.raises(tu.TransportError, match="symlink"):
        await t.connect(boxes.PORT_RUNTURN)
    (t.host_path(5557)).write_text("x")
    with pytest.raises(tu.TransportError, match="not a socket"):
        await t.connect(boxes.PORT_SHELL)
    v.close()


async def test_connect_refuses_the_wrong_peer_uid(short_dir):
    box = make_box(short_dir)
    t = box.transport
    t.host_dir.mkdir(parents=True)
    srv = guest_unix.listen(5556, str(t.host_path(5556)))
    try:
        if tu.peer_uid(socket.socketpair()[0]) is None:
            pytest.skip("no peer credentials on this platform")
        with pytest.raises(tu.TransportError, match="peer uid"):
            await tu.connect_checked(t.host_path(5556), expected_uid=os.getuid() + 1)
        s = await tu.connect_checked(t.host_path(5556), expected_uid=os.getuid())
        s.close()
    finally:
        srv.close()


def test_in_container_forwarder_splices_tcp_to_the_proxy_socket(short_dir):
    """bootstrap's 127.0.0.1 forwarder -> proxy.sock (the only way out)."""
    import threading
    proxy_path = str(short_dir / "proxy.sock")
    up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    up.bind(proxy_path)
    up.listen(1)

    def fake_proxy():
        c, _ = up.accept()
        req = c.recv(4096)
        c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n" + req[:7])
        c.close()
    threading.Thread(target=fake_proxy, daemon=True).start()
    ready = threading.Event()
    addr = []
    threading.Thread(target=bootstrap.forwarder, daemon=True,
                     kwargs={"addr": ("127.0.0.1", 0), "proxy_path": proxy_path,
                             "ready": lambda a: (addr.append(a), ready.set())}).start()
    assert ready.wait(3)
    c = socket.create_connection(addr[0], timeout=3)
    c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
    got = b""
    while True:
        b = c.recv(4096)
        if not b:
            break
        got += b
    c.close()
    up.close()
    assert got.startswith(b"HTTP/1.1 200") and got.endswith(b"CONNECT")
    assert bootstrap.proxy_env()["HTTPS_PROXY"] == "http://127.0.0.1:8443"


# --- lifecycle (fake docker) -----------------------------------------------------------

class FakeCLI:
    def __init__(self, info=None, run_rc=0, on_run=None):
        self.calls = []
        self.info = info or HARDENED_INFO
        self.run_rc = run_rc
        self.on_run = on_run
        self.alive = False

    async def run(self, *args, timeout=120):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "info":
            return 0, json.dumps(self.info), ""
        if cmd == "run":
            if self.run_rc:
                return self.run_rc, "", "boom"
            self.alive = True
            if self.on_run:
                await self.on_run()
            return 0, "c0ffee\n", ""
        if cmd == "inspect":
            fmt = args[2]
            if "Pid" in fmt:
                return 0, "4242\n", ""
            if "jav3.box" in fmt:
                # leftovers._containers: jav3-p-other is another install's
                # (its socket mount is not under this server's vm_dir)
                def row(name, src):
                    return (f"/{name}\t1\t{name[5:]}\texited\t"
                            + json.dumps([{"Type": "bind", "Source": src}]))
                mine = str(settings.vm_dir / "sock" / "10")
                return 0, "\n".join([row("jav3-p-alpha", mine), row("jav3-p-ghost", mine),
                                     row("jav3-p-other", "/elsewhere/sock/10")]) + "\n", ""
            return (0, "true\n", "") if self.alive else (1, "", "no such container")
        if cmd == "rm":
            self.alive = False
            return 0, "", ""
        if cmd == "stats":
            return 0, json.dumps({"MemUsage": "12.5MiB / 512MiB", "CPUPerc": "3.25%",
                                  "PIDs": "7"}) + "\n", ""
        if cmd == "ps":
            return 0, "jav3-p-alpha\njav3-p-ghost\njav3-p-other\n", ""
        return 0, "", ""

    def ran(self, cmd):
        return [c for c in self.calls if c[0] == cmd]


@pytest.fixture
def harness(short_dir, dsettings, monkeypatch):
    """A docker box whose 'container' is a guest listener in this process."""
    from backend.vm import gateway_server
    events, hooks, acls = [], [], []
    box = make_box(short_dir)
    guest = {}

    async def on_run():
        t = box.transport
        guest["srv"] = guest_unix.listen(5556, str(t.host_path(5556)))

    fake = FakeCLI(on_run=on_run)
    monkeypatch.setattr(dr, "cli", fake)
    monkeypatch.setattr(dr, "guest_host_uid", lambda iso: os.getuid())

    async def ev(kind, summary, severity, box, detail):
        events.append((kind, severity))
    monkeypatch.setattr(dr, "security_event", ev)

    async def fake_acl(path, spec, op="-m"):
        acls.append((path.name, spec, op))
    monkeypatch.setattr(dr, "acl", fake_acl)

    async def handle_conn(loop, conn, *, peer_cid=None, box=None):
        conn.close()
    monkeypatch.setattr(gateway_server, "handle_conn", handle_conn)

    async def hook(event, b):
        hooks.append((event, b.id))
    monkeypatch.setattr(boxes, "_hooks", [hook])
    monkeypatch.setattr(boxes, "_emit", _quiet_emit)
    yield {"box": box, "cli": fake, "events": events, "hooks": hooks, "acls": acls}
    if "srv" in guest:
        guest["srv"].close()


async def _quiet_emit(event, box):
    for fn in list(boxes._hooks):
        await fn(event, box)


async def test_lifecycle_happy_path(harness):
    box, fake, hooks = harness["box"], harness["cli"], harness["hooks"]
    ctl = boxes.controller(box)
    assert isinstance(ctl, dr.DockerBox) and ctl.state == "stopped"
    await ctl.acquire()
    assert ctl.state == "running" and ctl.inflight == 1 and ctl.pid == 4242
    assert ctl.running()
    run = fake.ran("run")[0]
    assert flag_values(run, "--network") == ["none"]
    assert hooks == [("box_up", "p-alpha")]
    t = box.transport
    assert t.gateway_path().is_socket() and t.proxy_path().is_socket()
    for d in (t.host_dir.parent, t.host_dir):
        assert os.stat(d).st_mode & 0o777 == 0o700
    # box_up ran BEFORE the container started (the proxy is up first)
    assert fake.calls.index(run) > fake.calls.index(fake.ran("info")[0])
    ctl.release()
    assert ctl.inflight == 0 and ctl.idle_since is not None
    st = await ctl.stats()
    assert st == {"rss_bytes": int(12.5 * 1024 ** 2), "cpu_pct": 3.25, "pids": 7}
    await ctl.acquire()                      # alive: no second docker run
    ctl.release()
    assert len(fake.ran("run")) == 1
    await boxes.stop(box)
    assert ctl.state == "stopped" and not ctl.running()
    assert hooks[-1] == ("box_down", "p-alpha")
    assert not t.gateway_path().exists() and not t.proxy_path().exists()
    assert fake.ran("rm")[-1] == ["rm", "--force", "jav3-p-alpha"]


async def test_lifecycle_restarts_a_dead_container(harness):
    box, fake = harness["box"], harness["cli"]
    ctl = boxes.controller(box)
    await ctl.acquire()
    ctl.release()
    fake.alive = False                       # the container died underneath
    await ctl.acquire()
    ctl.release()
    assert len(fake.ran("run")) == 2 and ctl.state == "running"
    await ctl.teardown()


async def test_lifecycle_run_failure_is_failed_and_clean(harness):
    box, fake, hooks = harness["box"], harness["cli"], harness["hooks"]
    fake.run_rc = 125
    ctl = dr.DockerBox(box)
    box.ctl = ctl
    with pytest.raises(dr.DockerError, match="docker run failed"):
        await ctl.acquire()
    assert ctl.state == "failed" and ctl.inflight == 0 and "boom" in ctl.error
    assert hooks == [("box_up", "p-alpha"), ("box_down", "p-alpha")]
    assert not box.transport.gateway_path().exists()
    await ctl.teardown()
    assert ctl.state == "stopped"


async def test_box_up_hook_failure_blocks_the_container(harness, monkeypatch):
    box, fake = harness["box"], harness["cli"]

    async def bad(event, b):
        raise RuntimeError("proxy listener failed")
    monkeypatch.setattr(boxes, "_hooks", [bad])
    ctl = dr.DockerBox(box)
    with pytest.raises(RuntimeError):
        await ctl.boot()
    assert ctl.state == "failed" and fake.ran("run") == []


async def test_not_ready_in_time_fails(harness, monkeypatch):
    box, fake = harness["box"], harness["cli"]

    async def no_guest():
        pass
    fake.on_run = no_guest
    monkeypatch.setattr(settings, "vm_boot_timeout_seconds", 1)
    ctl = dr.DockerBox(box)
    with pytest.raises(dr.DockerError, match="did not become ready"):
        await ctl.acquire()
    assert ctl.state == "failed" and not fake.alive


def test_state_machine_rejects_bad_transitions(short_dir):
    ctl = dr.DockerBox(make_box(short_dir))
    with pytest.raises(dr.DockerError):
        ctl._to("running")                   # stopped -> running skips starting
    ctl._to("starting")
    ctl._to("running")
    with pytest.raises(dr.DockerError):
        ctl._to("starting")
    ctl._to("stopping")
    ctl._to("stopped")


async def test_refusal_when_require_settings_on(harness, monkeypatch):
    box, fake, events = harness["box"], harness["cli"], harness["events"]
    fake.info = {**HARDENED_INFO, "SecurityOptions": ["name=seccomp,profile=builtin"]}
    set_req(monkeypatch, "docker_require_userns", True)
    ctl = dr.DockerBox(box)
    with pytest.raises(dr.DockerHardeningError):
        await ctl.acquire()
    assert fake.ran("run") == [] and ("docker_hardening_refused", "warn") in events
    assert ctl.state == "failed"


async def test_weak_isolation_is_loud_not_silent(harness):
    box, fake, events = harness["box"], harness["cli"], harness["events"]
    fake.info = {**HARDENED_INFO, "SecurityOptions": ["name=seccomp,profile=builtin"]}
    ctl = dr.DockerBox(box)
    await ctl.acquire()
    ctl.release()
    assert ("docker_weak_isolation", "warn") in events
    assert ctl.status()["isolation"]["weak"] is True
    await ctl.teardown()


async def test_docker_disabled_refuses(harness, monkeypatch):
    monkeypatch.setattr(settings, "docker_enabled", False)
    ctl = dr.DockerBox(harness["box"])
    with pytest.raises(dr.DockerError, match="disabled"):
        await ctl.acquire()
    assert harness["cli"].calls == []


async def test_gateway_without_box_identity_is_refused(short_dir, monkeypatch):
    from backend.vm import gateway_server

    async def old(loop, conn):
        pass
    monkeypatch.setattr(gateway_server, "handle_conn", old)
    with pytest.raises(dr.DockerError, match="box= identity"):
        dr.gateway_handler(make_box(short_dir))


async def test_prepare_sock_dir_acl_for_the_container_uid(short_dir, monkeypatch):
    acls = []

    async def fake_acl(path, spec, op="-m"):
        acls.append((path.name, spec, op))
    monkeypatch.setattr(dr, "acl", fake_acl)
    box = make_box(short_dir)
    t = box.transport
    t.host_dir.mkdir(parents=True)
    os.symlink("/etc/passwd", t.host_path(5556))     # stale guest junk
    (t.host_dir / "gateway.sock").mkdir()             # a guest squatting a name
    (t.host_dir / "gateway.sock" / "x").write_text("x")
    await dr.prepare_sock_dir(box, 110000)
    assert acls == [("10", "u:110000:rwx", "-m"),
                    ("10", f"u:110000:rw-,u:{os.getuid()}:rw-", "-dm")]
    assert list(t.host_dir.iterdir()) == []
    assert os.path.exists("/etc/passwd")


async def test_reap_orphans(harness):
    fake = harness["cli"]
    gone = await dr.reap_orphans()
    assert gone == ["jav3-p-alpha", "jav3-p-ghost"]      # none registered+running
    assert ["rm", "--force", "jav3-p-ghost"] in fake.calls
    assert ["rm", "--force", "jav3-p-other"] not in fake.calls   # another install's


def test_allocate_refuses_docker_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "docker_enabled", False)
    with pytest.raises(boxes.BoxError, match="docker_enabled"):
        boxes.registry.allocate("project", project="alpha", runtime="docker")


# --- recipe adapter ----------------------------------------------------------------------

def test_recipe_renders_a_dockerfile():
    df = docker_recipe.render_dockerfile({
        "name": "dev", "from": "main",
        "packages": [{"manager": "apt", "package": "libpq-dev", "version": None},
                     {"manager": "pip", "package": "psycopg", "version": "3.2.1"},
                     {"manager": "npm", "package": "@scope/tool", "version": "1.0.0"}]})
    assert df.splitlines()[1] == f"FROM {settings.docker_image_turn}"
    assert "libpq-dev" in df and "psycopg==3.2.1" in df and "@scope/tool@1.0.0" in df
    assert df.rstrip().endswith("USER 10001:10001")
    assert "chmod a-s" in df


@pytest.mark.parametrize("pkg", [
    {"manager": "apt", "package": "x; rm -rf /"},
    {"manager": "pip", "package": "ok", "version": "1 && curl evil"},
    {"manager": "brew", "package": "x"},
])
def test_recipe_refuses_injection(pkg):
    with pytest.raises(docker_recipe.RecipeError):
        docker_recipe.render_dockerfile({"name": "dev", "packages": [pkg]})


# --- availability (WP6: GET /api/vm/boxes `runtimes`) ---------------------------------

async def test_availability_off_and_on(dsettings, monkeypatch):
    monkeypatch.setattr(settings, "docker_enabled", False)
    off = await dr.availability(refresh=True)
    assert off["available"] is False and "docker_enabled" in off["reason"]
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(dr, "cli", FakeCLI(info={**HARDENED_INFO,
                                                 "Runtimes": {"runc": {}, "runsc": {}}}))
    monkeypatch.setattr(dr.shutil, "which", lambda n: "/usr/bin/" + n)
    on = await dr.availability(refresh=True)
    assert on["available"] and on["rootless"] and on["gvisor"] and not on["weak"]
    monkeypatch.setattr(dr, "cli", FakeCLI(info={**HARDENED_INFO,
                                                 "SecurityOptions": []}))
    bad = await dr.availability(refresh=True)
    assert bad["available"] is False and "seccomp" in bad["reason"]
    rt = await dr.runtimes_json()
    assert set(rt) == {"kvm", "docker"} and "available" in rt["kvm"]


def test_missing_memory_cgroup_is_a_warning():
    # Raspberry Pi OS boots with cgroup_disable=memory: --memory is silently ignored
    from backend.vm import docker_runtime as d
    info = d.DaemonInfo(seccomp=True, raw={"MemoryLimit": False})
    assert d.NO_MEMORY_LIMIT in d.plan_isolation(info).warnings
    ok = d.DaemonInfo(seccomp=True, raw={"MemoryLimit": True})
    assert d.NO_MEMORY_LIMIT not in d.plan_isolation(ok).warnings


async def test_destroy_removes_the_box_socket_dir(harness, monkeypatch):
    """A destroyed docker box (idle reaper, operator) takes its sock/<cid>
    directory with it; a stopped one keeps it for the next start. The leftovers
    scan lists a sock dir no box uses, so the reaper used to create one per box."""
    import contextlib
    from backend.vm import boxlog
    box = harness["box"]
    ctl = boxes.controller(box)
    d = box.transport.host_dir

    @contextlib.asynccontextmanager
    async def quiet(*a, **k):
        yield
    monkeypatch.setattr(boxlog, "action", quiet)
    await ctl.acquire()
    ctl.release()
    await boxes.stop(box)
    assert d.is_dir()                              # stopped: the slot stays
    await boxes.destroy(box)
    assert not d.exists()
    assert d.parent.is_dir()                       # only this box's slot


async def test_forget_leaves_a_running_boxs_dir(harness):
    box = harness["box"]
    ctl = boxes.controller(box)
    await ctl.acquire()
    await ctl.forget()
    assert box.transport.host_dir.is_dir() and box.transport.gateway_path().exists()
    ctl.release()
    await boxes.stop(box)
