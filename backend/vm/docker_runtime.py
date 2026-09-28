"""Docker box runtime (WP8): a hardened container instead of a KVM guest.

The operator's lighter, weaker option ("a lot lighter, less secure, but people
may not always care"), chosen explicitly per security profile
(`box_runtime: "docker"`) and only while `settings.docker_enabled`. Registers
itself as `boxes.register_runtime("docker", DockerBox)` on import.

The guarantees this module holds (docs/docker-runtime.md has the reasoning):

  * The container has NO network interface but `lo` (`--network none`). Its
    only ways out are two host-owned AF_UNIX sockets in a read-only mount:
    the gateway (op-gated by box kind) and the box's egress proxy. So "the
    egress proxy is the only way out" holds by construction, not by firewall
    rules: there is no interface for a packet to leave by.
  * Every run spec passes `validate_spec` before `docker run`: no docker
    socket, no privileged, no added caps, no published ports, no host
    namespaces, no mounts but the box's socket dirs and its /srv volume.
  * The daemon is probed first. No seccomp: refused, always. Neither rootless
    nor userns-remap: a loud warning (security event), or a refusal when
    `docker_require_userns` is on. gVisor (runsc) is used when present, and
    required when `docker_oci_runtime == "runsc"` or `docker_require_runsc`.

Nothing here runs unless a docker box is started; with docker_enabled off,
boxes.allocate refuses runtime "docker" before this module is reached.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import settings
from . import boxes, transport_unix
from .transport_unix import UnixListener, UnixTransport, connect_checked

GUEST_UID = 10001                    # the image's only user (vm/docker/Dockerfile)
GUEST_GID = 10001
LABEL = "jav3.managed"
_EXTRA_LABELS: dict[str, str] = {}   # tests / local smoke runs add jav3.test=wp8
RUN_TMPFS_MB = 8
HOME_TMPFS_MB = 64
SHM_MB = 16

STATES = ("stopped", "starting", "running", "stopping", "failed")
_TRANSITIONS = {
    "stopped": {"starting"},
    "starting": {"running", "failed", "stopping"},
    "running": {"stopping", "failed"},
    "stopping": {"stopped"},
    "failed": {"stopping", "starting"},
}


class DockerError(boxes.BoxError):
    """The docker runtime cannot do what was asked (daemon down, run failed)."""


class DockerHardeningError(DockerError):
    """A hardening prerequisite is missing and the settings say to refuse."""


def _setting(name: str, default):
    """docker_require_runsc / docker_require_userns (config.py)."""
    return getattr(settings, name, default)


# --- the daemon ---------------------------------------------------------------

@dataclass
class DaemonInfo:
    rootless: bool = False
    userns: bool = False
    seccomp: bool = False
    seccomp_profile: str | None = None
    apparmor: bool = False
    selinux: bool = False
    runtimes: tuple[str, ...] = ()
    server_version: str = ""
    cgroup_version: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, info: dict) -> "DaemonInfo":
        opts = info.get("SecurityOptions") or []
        names: dict[str, dict] = {}
        for o in opts:
            kv = dict(p.split("=", 1) for p in str(o).split(",") if "=" in p)
            if "name" in kv:
                names[kv["name"]] = kv
        return cls(rootless="rootless" in names, userns="userns" in names,
                   seccomp="seccomp" in names,
                   seccomp_profile=(names.get("seccomp") or {}).get("profile"),
                   apparmor="apparmor" in names, selinux="selinux" in names,
                   runtimes=tuple(sorted((info.get("Runtimes") or {}).keys())),
                   server_version=str(info.get("ServerVersion") or ""),
                   cgroup_version=str(info.get("CgroupVersion") or ""), raw=info)


class DockerCLI:
    """The docker CLI, argv only (never a shell). Talks to whatever daemon the
    service user's docker context names; the socket is never exposed to a box.
    Tests swap `docker_runtime.cli` for a fake with the same `run`."""

    def __init__(self, binary: str | None = None):
        self.binary = binary

    async def run(self, *args: str, timeout: float = 120) -> tuple[int, str, str]:
        binary = self.binary or settings.docker_bin
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except (FileNotFoundError, PermissionError) as e:
            return 127, "", f"{binary}: {e}"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "", f"docker {args[0] if args else ''}: timed out"
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


cli = DockerCLI()


async def probe() -> DaemonInfo:
    rc, out, err = await cli.run("info", "--format", "{{json .}}", timeout=20)
    if rc != 0:
        raise DockerError(f"docker daemon unavailable: {err.strip()[:200]}")
    try:
        return DaemonInfo.parse(json.loads(out))
    except (ValueError, TypeError) as e:
        raise DockerError(f"docker info unreadable: {e}") from e


@dataclass
class Isolation:
    """What the daemon gives this box, decided once per boot."""
    oci_runtime: str | None              # "runsc" or None (the daemon's default)
    userns: str                          # "rootless" | "userns-remap" | "none"
    warnings: list[str]
    apparmor: bool = False               # daemon has AppArmor: name the profile

    @property
    def weak(self) -> bool:
        return self.userns == "none"


# `docker run --memory` is silently ignored when the kernel has the memory
# cgroup off — Raspberry Pi OS boots with cgroup_disable=memory (2026-09-27)
NO_MEMORY_LIMIT = ("no memory limits: this kernel has the memory cgroup off, so a "
                   "box can use all of the host's RAM. On a Raspberry Pi add "
                   "'cgroup_enable=memory cgroup_memory=1' to /boot/firmware/cmdline.txt "
                   "and reboot")


def plan_isolation(info: DaemonInfo) -> Isolation:
    """Decide runtime + user namespacing, or refuse. Pure (settings only)."""
    warnings: list[str] = []
    if not info.seccomp or (info.seccomp_profile or "").lower() == "unconfined":
        raise DockerHardeningError(
            "the docker daemon runs without seccomp; docker boxes are refused")
    want = (settings.docker_oci_runtime or "").strip()
    require_runsc = want == "runsc" or bool(_setting("docker_require_runsc", False))
    has_runsc = "runsc" in info.runtimes
    if require_runsc and not has_runsc:
        raise DockerHardeningError(
            "gVisor (runsc) is required but not registered with the docker daemon")
    if want and want not in ("runsc", "runc"):
        raise DockerHardeningError(f"unknown docker_oci_runtime {want!r}")
    oci = "runsc" if (has_runsc and want != "runc") else None
    if oci is None:
        warnings.append("no gVisor: the container shares the host kernel's full "
                        "syscall surface (filtered by seccomp only)")
    userns = "rootless" if info.rootless else ("userns-remap" if info.userns else "none")
    if userns == "none":
        msg = ("docker is neither rootless nor userns-remapped: container uid "
               f"{GUEST_UID} is host uid {GUEST_UID}, and root in the container "
               "(after any escape) is host root")
        if _setting("docker_require_userns", False):
            raise DockerHardeningError(msg)
        warnings.append(msg)
    if info.raw.get("MemoryLimit") is False:
        warnings.append(NO_MEMORY_LIMIT)
    return Isolation(oci_runtime=oci, userns=userns, warnings=warnings,
                     apparmor=info.apparmor)


SUBUID = Path("/etc/subuid")


def _subuid_start(*names: str) -> int | None:
    """First subordinate uid of the first /etc/subuid line naming one of
    `names` (a user name or a numeric uid)."""
    try:
        lines = SUBUID.read_text().splitlines()
    except OSError:
        return None
    for ln in lines:
        parts = ln.strip().split(":")
        if len(parts) == 3 and parts[0] in names:
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def guest_host_uid(iso: Isolation) -> int:
    """The host uid the container's GUEST_UID runs as (for the socket-dir ACL
    and the peer check). Verified against the live process after start."""
    if iso.userns == "rootless":
        import getpass
        start = _subuid_start(getpass.getuser(), str(os.getuid()))
        return (start + GUEST_UID - 1) if start is not None else GUEST_UID
    if iso.userns == "userns-remap":
        start = _subuid_start("dockremap")
        return (start + GUEST_UID) if start is not None else GUEST_UID
    return GUEST_UID


# --- images ---------------------------------------------------------------------

_image_resolvers: list[Callable[["boxes.Box"], str | None]] = []


def add_image_resolver(fn: Callable[["boxes.Box"], str | None]) -> None:
    """WP5: map a docker box's (variant, version) to an image reference.
    First non-None wins. Builtin: variant 'main' -> the kind's setting."""
    _image_resolvers.append(fn)


def image_for(box: "boxes.Box") -> str:
    for fn in _image_resolvers:
        ref = fn(box)
        if ref:
            return ref
    if box.image[0] != "main":
        # a variant rendered by docker_recipe; `docker run` fails (DockerError)
        # if it was never built
        from .docker_recipe import variant_image
        return variant_image(box.image[0])
    return {"service": settings.docker_image_svc,
            "builder": settings.docker_image_builder}.get(box.kind,
                                                          settings.docker_image_turn)


# --- the run spec -------------------------------------------------------------------

def container_name(box: "boxes.Box") -> str:
    return f"jav3-{box.id}"


def srv_volume(box: "boxes.Box") -> str:
    return f"jav3-srv-{box.id}"


def _jarvis_tmpfs_mb(box: "boxes.Box") -> int:
    # the pushed package + workspace copy live here; tmpfs pages count against
    # the container's memory limit, so it is capped at half of it
    return max(64, int(box.mem_mb) // 2)


def run_spec(box: "boxes.Box", iso: Isolation) -> list[str]:
    """`docker run` argv (without the binary) for one box. Pure."""
    t = box.transport
    assert isinstance(t, UnixTransport), "docker boxes use the unix transport"
    mem = int(box.mem_mb)
    labels = {LABEL: "1", "jav3.box": box.id, "jav3.kind": box.kind,
              **({"jav3.project": box.project} if box.project else {}),
              **_EXTRA_LABELS}
    argv = ["run", "--detach", "--name", container_name(box),
            "--hostname", box.id]
    for k, v in labels.items():
        argv += ["--label", f"{k}={v}"]
    argv += [
        "--user", f"{GUEST_UID}:{GUEST_GID}",
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",
        "--read-only",
        "--tmpfs", f"/tmp:rw,size={settings.docker_tmpfs_mb}m,mode=1777,noexec,nosuid,nodev",
        "--tmpfs", f"/run:rw,size={RUN_TMPFS_MB}m,mode=0755,noexec,nosuid,nodev",
        "--tmpfs", (f"/home/jav3:rw,size={HOME_TMPFS_MB}m,mode=0700,"
                    f"uid={GUEST_UID},gid={GUEST_GID},nosuid,nodev"),
        "--tmpfs", (f"/opt/jarvis:rw,size={_jarvis_tmpfs_mb(box)}m,mode=0700,"
                    f"uid={GUEST_UID},gid={GUEST_GID},nosuid,nodev"),
        "--shm-size", f"{SHM_MB}m",
        "--pids-limit", str(int(settings.docker_box_pids)),
        "--memory", f"{mem}m", "--memory-swap", f"{mem}m",
        "--cpus", str(settings.docker_box_cpus),
        "--ulimit", "core=0", "--ulimit", "nofile=1024:4096",
        "--oom-score-adj", "500",
        "--ipc", "private", "--cgroupns", "private",
        "--restart", "no", "--stop-timeout", "5",
        "--log-driver", "json-file", "--log-opt", "max-size=1m",
        "--log-opt", "max-file=2",
        "--no-healthcheck",
    ]
    if iso.oci_runtime:
        argv += ["--runtime", iso.oci_runtime]
    if iso.apparmor:
        argv += ["--security-opt", "apparmor=docker-default"]
    argv += ["--mount", f"type=bind,src={t.host_dir},dst={transport_unix.GUEST_ROOT}"]
    if box.kind == "service":
        argv += ["--mount", f"type=volume,src={srv_volume(box)},dst=/srv"]
    for k, v in (("JAV3_TRANSPORT", "unix"), ("JAV3_BOX_ID", box.id),
                 ("JAV3_BOX_KIND", box.kind), ("HOME", "/home/jav3")):
        argv += ["--env", f"{k}={v}"]
    argv.append(image_for(box))
    return argv


# flags that must be present, as (flag, value) pairs
REQUIRED = (("--network", "none"), ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges=true"), ("--read-only", None),
            ("--pids-limit", None), ("--memory", None), ("--memory-swap", None),
            ("--cpus", None), ("--user", f"{GUEST_UID}:{GUEST_GID}"))
FORBIDDEN_FLAGS = ("--privileged", "--cap-add", "--device", "-p", "--publish",
                   "-P", "--publish-all", "-v", "--volume", "--volumes-from",
                   "--pid", "--uts", "--userns", "--network-alias", "--link",
                   "--add-host", "--group-add", "--device-cgroup-rule",
                   "--gpus", "--sysctl")
_BAD_SECOPT = re.compile(r"^(seccomp=unconfined|apparmor=unconfined|label[:=]disable|"
                         r"systempaths=unconfined|no-new-privileges=false)$")


def validate_spec(argv: list[str], box: "boxes.Box") -> None:
    """Refuse a run spec that breaks any hardening rule. Runs on every boot,
    after run_spec, so a later edit to run_spec cannot quietly weaken a box."""
    def values(flag: str) -> list[str]:
        out = []
        for i, a in enumerate(argv):
            if a == flag and i + 1 < len(argv):
                out.append(argv[i + 1])
            elif a.startswith(flag + "="):
                out.append(a.split("=", 1)[1])
        return out

    for a in argv:
        head = a.split("=", 1)[0]
        if head in FORBIDDEN_FLAGS:
            raise DockerHardeningError(f"run spec: {head} is forbidden")
        if "docker.sock" in a or "containerd.sock" in a:
            raise DockerHardeningError("run spec: a daemon socket is never mounted")
    for flag, val in REQUIRED:
        got = values(flag) if val is not None else [a for a in argv if a == flag]
        if not got or (val is not None and val not in got):
            raise DockerHardeningError(f"run spec: {flag} {val or ''} missing")
    if values("--network") != ["none"]:
        raise DockerHardeningError("run spec: the only network is none")
    for so in values("--security-opt"):
        if _BAD_SECOPT.match(so):
            raise DockerHardeningError(f"run spec: --security-opt {so} is forbidden")
    for ns in ("--ipc", "--cgroupns"):
        if values(ns) != ["private"]:
            raise DockerHardeningError(f"run spec: {ns} must be private")
    t = box.transport
    allowed = {f"type=bind,src={t.host_dir},dst={transport_unix.GUEST_ROOT}"}
    if box.kind == "service":
        allowed.add(f"type=volume,src={srv_volume(box)},dst=/srv")
    for m in values("--mount"):
        if m not in allowed:
            raise DockerHardeningError(f"run spec: mount {m!r} is not allowed")
    for tm in values("--tmpfs"):
        dst, _, opts = tm.partition(":")
        o = set(opts.split(","))
        if not any(x.startswith("size=") for x in o) or not {"nosuid", "nodev"} <= o:
            raise DockerHardeningError(f"run spec: tmpfs {dst} needs size, nosuid, nodev")
        if dst in ("/tmp", "/run") and "noexec" not in o:
            raise DockerHardeningError(f"run spec: tmpfs {dst} must be noexec")
    if not {"/tmp", "/run"} <= {tm.partition(":")[0] for tm in values("--tmpfs")}:
        raise DockerHardeningError("run spec: /tmp and /run must be capped tmpfs")


# --- socket directory -------------------------------------------------------------

async def _setfacl(path: Path, spec: str, op: str = "-m") -> None:
    tool = shutil.which("setfacl")
    if tool is None:
        raise DockerHardeningError(
            "setfacl not found (install the 'acl' package): the per-box socket "
            "directory stays 0700 and the container could not reach it")
    proc = await asyncio.create_subprocess_exec(
        tool, op, spec, str(path), stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        raise DockerError(f"setfacl {path.name}: {err.decode(errors='replace')[:200]}")


acl = _setfacl                         # tests swap this


async def prepare_sock_dir(box: "boxes.Box", guest_uid: int) -> None:
    """<vm_dir>/sock and <vm_dir>/sock/<cid>: owned by the service user, 0700,
    and sock/<cid> emptied (everything in it is ephemeral, and whatever a
    previous guest left there is untrusted). The container's host uid gets one
    ACL entry on sock/<cid> (rwx: create its listeners). The DEFAULT ACL names
    both uids, rw: every socket created there, by either side, is then
    connectable by the other side and by nobody else, whatever mode its
    creator chmods it to (the gateway and boxinfo.listen use 0660, which
    leaves the ACL mask at rw). The container sees no other host path."""
    t = box.transport
    for d in (t.host_dir.parent, t.host_dir):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    _empty(t.host_dir)
    if guest_uid != os.getuid():
        await acl(t.host_dir, f"u:{guest_uid}:rwx")
        await acl(t.host_dir, f"u:{guest_uid}:rw-,u:{os.getuid()}:rw-", "-dm")


def _empty(d: Path) -> None:
    for p in list(d.iterdir()):
        if p.is_symlink() or not p.is_dir():
            p.unlink(missing_ok=True)
        else:
            shutil.rmtree(p, ignore_errors=True)      # never follows symlinks


def _proc_uid(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        for ln in Path(f"/proc/{pid}/status").read_text().splitlines():
            if ln.startswith("Uid:"):
                return int(ln.split()[2])          # effective
    except (OSError, ValueError, IndexError):
        return None
    return None


# --- host ends: gateway + proxy ------------------------------------------------------

def gateway_handler(box: "boxes.Box"):
    """The gateway's handle_conn bound to this box: identity = this listener.
    Used only when the gateway has no listen_unix of its own. Fails closed if
    handle_conn cannot take `box=` (contract D.1): without it the gateway could
    not gate ops by kind or refuse a foreign op_id."""
    from . import gateway_server
    hc = gateway_server.handle_conn
    if "box" not in inspect.signature(hc).parameters:
        raise DockerError("gateway_server.handle_conn has no box= identity (WP1); "
                          "docker boxes are refused until it does")

    async def h(loop, conn):
        await hc(loop, conn, box=box)
    return h


async def _gateway_listen(box: "boxes.Box") -> UnixListener | None:
    """WP1's `gateway.listen_unix(box)` when it exists (it binds
    <sock>/gateway.sock with the box as identity); else our own listener."""
    from . import gateway_server
    gw = gateway_server.gateway
    if hasattr(gw, "listen_unix") and "box" in inspect.signature(
            gateway_server.handle_conn).parameters:
        await gw.listen_unix(box)
        return None
    lst = UnixListener(box.transport.gateway_path(), gateway_handler(box), mode=0o660)
    await lst.start()
    return lst


async def _gateway_unlisten(box: "boxes.Box") -> None:
    from . import gateway_server
    gw = gateway_server.gateway
    if hasattr(gw, "unlisten_unix"):
        await gw.unlisten_unix(box.id)


def proxy_handler(box: "boxes.Box"):
    """The egress proxy for THIS box. Preferred: WP2's
    `egress_proxy.handle_box_conn(box, reader, writer)` (attribution = the
    box passed in). Fallback: splice to the box's TCP listener
    box.host_ip:vm_egress_proxy_port, which WP2 attributes by listener; if
    that listener is not there the connection just closes (no egress)."""
    from . import egress_proxy
    fn = getattr(egress_proxy, "handle_box_conn", None)
    if fn is not None:
        async def direct(r, w):
            await fn(box, r, w)
        return transport_unix.stream_handler(direct)

    async def tcp(r, w):
        try:
            r2, w2 = await asyncio.wait_for(asyncio.open_connection(
                box.host_ip, settings.vm_egress_proxy_port), 10)
        except (OSError, asyncio.TimeoutError):
            return
        await transport_unix.splice(r, w, r2, w2)
    return transport_unix.stream_handler(tcp)


# --- the controller ------------------------------------------------------------------

async def _security_event(kind: str, summary: str, severity: str, box, detail) -> None:
    try:
        from .. import security
        from ..db import get_db
        db = await get_db()
        try:
            await security.raise_event(db, kind=kind, summary=summary, severity=severity,
                                       project=box.project, detail=detail)
        finally:
            await db.close()
    except Exception as e:  # noqa: BLE001 — an audit hiccup must not wedge a boot
        print(f"[docker] security event {kind} not recorded: {e}")


security_event = _security_event       # tests swap this


class DockerBox:
    """GuestVM's interface for one docker box (boxes.register_runtime)."""

    def __init__(self, box: "boxes.Box"):
        if box.runtime != "docker":
            raise DockerError(f"{box.id} is not a docker box")
        self.box = box
        self.state = "stopped"
        self.error: str | None = None
        self.container_id: str | None = None
        self.pid: int | None = None
        self.guest_uid: int | None = None
        self.isolation: Isolation | None = None
        self.booted_at: float | None = None
        self.idle_since: float | None = None
        self._inflight = 0
        self._lock = asyncio.Lock()
        self._listeners: list[UnixListener] = []
        self._gw_listening = False
        self._hooked = False

    # GuestVM interface -------------------------------------------------------
    @property
    def inflight(self) -> int:
        return self._inflight

    def running(self) -> bool:
        return self.state in ("starting", "running")

    def _to(self, state: str) -> None:
        if state not in _TRANSITIONS[self.state]:
            raise DockerError(f"{self.box.id}: {self.state} -> {state} is not a transition")
        self.state = state

    async def acquire(self) -> None:
        async with self._lock:
            if self.state == "running" and not await self._alive():
                await self._teardown_locked()
            if self.state in ("stopped", "failed"):
                await self._boot_locked()
            if self.state == "starting":
                await self._wait_ready()
            self._inflight += 1

    def release(self) -> None:
        self._inflight = max(0, self._inflight - 1)
        if self._inflight == 0:
            self.idle_since = time.monotonic()

    async def boot(self) -> None:
        async with self._lock:
            if not self.running():
                await self._boot_locked()

    async def teardown(self) -> None:
        async with self._lock:
            await self._teardown_locked()

    # boot / teardown -----------------------------------------------------------
    async def _boot_locked(self) -> None:
        if not settings.docker_enabled:
            raise DockerError("docker runtime is disabled (docker_enabled)")
        self._to("starting")
        self.error = None
        try:
            info = await probe()
            try:
                iso = plan_isolation(info)
            except DockerHardeningError as e:
                await security_event("docker_hardening_refused", str(e), "warn",
                                     self.box, {"box": self.box.id})
                raise
            self.isolation = iso
            if iso.warnings:
                await security_event(
                    "docker_weak_isolation" if iso.weak else "docker_isolation_note",
                    f"docker box {self.box.id}: " + "; ".join(iso.warnings),
                    "warn" if iso.weak else "info", self.box,
                    {"box": self.box.id, "userns": iso.userns,
                     "oci_runtime": iso.oci_runtime or "runc"})
            self.guest_uid = guest_host_uid(iso)
            argv = run_spec(self.box, iso)
            validate_spec(argv, self.box)
            await prepare_sock_dir(self.box, self.guest_uid)
            await self._start_listeners()
            await boxes.box_up(self.box)      # WP2 et al.; raising fails closed
            self._hooked = True
            await cli.run("rm", "--force", container_name(self.box), timeout=30)
            rc, out, err = await cli.run(*argv, timeout=120)
            if rc != 0:
                raise DockerError(f"docker run failed: {err.strip()[:300]}")
            self.container_id = out.strip()[:64] or None
            self.pid = await self._inspect_pid()
            observed = _proc_uid(self.pid)
            if observed is not None and observed != self.guest_uid:
                # the mapping was computed wrong: move the ACL to the real uid
                # (the guest retries its gateway connect meanwhile)
                t = self.box.transport
                if self.guest_uid != os.getuid():
                    await acl(t.host_dir, f"u:{self.guest_uid}", "-x")
                self.guest_uid = observed
                if observed != os.getuid():
                    await acl(t.host_dir, f"u:{observed}:rwx")
            self.booted_at = time.monotonic()
            self.idle_since = time.monotonic()
        except BaseException as e:
            self.error = str(e)
            self.state = "failed"
            await self._cleanup()
            raise

    async def _start_listeners(self) -> None:
        t = self.box.transport
        self._gw_listening = True
        own = await _gateway_listen(self.box)
        if own is not None:
            self._listeners.append(own)
        lst = UnixListener(t.proxy_path(), proxy_handler(self.box), mode=0o660)
        await lst.start()
        self._listeners.append(lst)

    async def _wait_ready(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.vm_boot_timeout_seconds
        path = self.box.transport.host_path(boxes.PORT_RUNTURN)
        while loop.time() < deadline:
            try:
                s = await connect_checked(path, expected_uid=self.guest_uid)
                s.close()
                self._to("running")
                return
            except transport_unix.TransportError as e:
                self.error = str(e)
                self.state = "failed"
                await self._cleanup()
                await security_event("docker_socket_refused", str(e), "warn",
                                     self.box, {"box": self.box.id})
                raise
            except OSError:
                if not await self._alive():
                    break
                await asyncio.sleep(0.5)
        self.error = "run-turn server did not become ready"
        self.state = "failed"
        await self._cleanup()
        raise DockerError(f"{self.box.id}: {self.error}")

    async def _teardown_locked(self) -> None:
        if self.state == "stopped":
            return
        if self.state != "stopping":
            self._to("stopping")
        await self._cleanup()
        self.state = "stopped"

    async def _cleanup(self) -> None:
        await cli.run("rm", "--force", container_name(self.box), timeout=60)
        for lst in self._listeners:
            await lst.stop()
        self._listeners.clear()
        if self._gw_listening:
            self._gw_listening = False
            await _gateway_unlisten(self.box)
        if self._hooked:
            self._hooked = False
            await boxes.box_down(self.box)
        t = self.box.transport
        if t.host_dir.is_dir():
            try:
                _empty(t.host_dir)
            except OSError:
                pass
        self.container_id = None
        self.pid = None
        self.booted_at = None

    # docker queries ---------------------------------------------------------------
    async def _inspect_pid(self) -> int | None:
        rc, out, _ = await cli.run("inspect", "--format", "{{.State.Pid}}",
                                   container_name(self.box), timeout=20)
        try:
            return int(out.strip()) if rc == 0 and int(out.strip()) > 0 else None
        except ValueError:
            return None

    async def _alive(self) -> bool:
        rc, out, _ = await cli.run("inspect", "--format", "{{.State.Running}}",
                                   container_name(self.box), timeout=20)
        return rc == 0 and out.strip() == "true"

    async def stats(self) -> dict:
        """{rss_bytes, cpu_pct, pids} from `docker stats` (works rootless,
        where /proc of the container pid may not be ours to read)."""
        if not self.running():
            return {"rss_bytes": None, "cpu_pct": None, "pids": None}
        rc, out, _ = await cli.run("stats", "--no-stream", "--format", "{{json .}}",
                                   container_name(self.box), timeout=20)
        if rc != 0:
            return {"rss_bytes": None, "cpu_pct": None, "pids": None}
        try:
            row = json.loads(out.strip().splitlines()[0])
        except (ValueError, IndexError):
            return {"rss_bytes": None, "cpu_pct": None, "pids": None}
        return {"rss_bytes": _bytes(str(row.get("MemUsage", "")).split("/")[0]),
                "cpu_pct": _pct(row.get("CPUPerc")),
                "pids": _int(row.get("PIDs"))}

    def status(self) -> dict:
        iso = self.isolation
        return {"runtime": "docker", "state": self.state, "error": self.error,
                "container": container_name(self.box), "inflight": self._inflight,
                "isolation": None if iso is None else {
                    "userns": iso.userns, "oci_runtime": iso.oci_runtime or "runc",
                    "weak": iso.weak, "warnings": iso.warnings}}


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _pct(v) -> float | None:
    try:
        return float(str(v).strip().rstrip("%"))
    except ValueError:
        return None


_UNITS = {"b": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3,
          "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3}


def _bytes(s: str) -> int | None:
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]+)\s*$", s or "")
    if not m or m.group(2).lower() not in _UNITS:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2).lower()])


# --- availability (for the UI: GET /api/vm/boxes `runtimes`) -------------------

_avail_cache: tuple[float, dict] | None = None
AVAIL_TTL = 30.0


async def availability(*, refresh: bool = False) -> dict:
    """Whether a docker box could start here, and how isolated it would be.
    Cheap: one `docker info`, cached AVAIL_TTL seconds.
    {available, reason?, rootless, userns, gvisor, seccomp, weak, warnings}"""
    global _avail_cache
    now = time.monotonic()
    if not refresh and _avail_cache and now - _avail_cache[0] < AVAIL_TTL:
        return _avail_cache[1]
    out = {"available": False, "reason": None, "rootless": None, "userns": None,
           "gvisor": None, "seccomp": None, "weak": None, "warnings": []}
    if not settings.docker_enabled:
        out["reason"] = "docker runtime is off (docker_enabled)"
    else:
        try:
            info = await probe()
            out.update(rootless=info.rootless, userns=info.rootless or info.userns,
                       gvisor="runsc" in info.runtimes, seccomp=info.seccomp)
            iso = plan_isolation(info)
            out.update(available=True, weak=iso.weak, warnings=iso.warnings)
            if shutil.which("setfacl") is None:
                out.update(available=False,
                           reason="setfacl not found (install the 'acl' package)")
        except DockerError as e:
            out["reason"] = str(e)
    _avail_cache = (now, out)
    return out


def kvm_availability() -> dict:
    from .gateway_server import gateway
    if not os.path.exists("/dev/kvm"):
        return {"available": False, "reason": "no /dev/kvm on this host"}
    if not gateway.enabled:
        return {"available": False, "reason": "vsock gateway not running"}
    return {"available": True, "reason": None}


async def runtimes_json() -> dict:
    """The `runtimes` object for GET /api/vm/boxes (WP6 greys docker out with
    `reason`)."""
    return {"kvm": kvm_availability(), "docker": await availability()}


async def reap_orphans() -> list[str]:
    """Remove jav3-managed containers no registered box owns (left over from an
    app restart: a docker box never outlives the app that started it)."""
    rc, out, _ = await cli.run("ps", "--all", "--filter", f"label={LABEL}=1",
                               "--format", "{{.Names}}", timeout=30)
    if rc != 0:
        return []
    live = {container_name(b) for b in boxes.all_boxes()
            if b.runtime == "docker" and b.ctl is not None and b.ctl.running()}
    gone = [n for n in out.split() if n.startswith("jav3-") and n not in live]
    for n in gone:
        await cli.run("rm", "--force", n, timeout=60)
    return gone


# --- registration ----------------------------------------------------------------------

def install() -> None:
    """Register the runtime and the hardened transport. Idempotent."""
    boxes.register_runtime("docker", DockerBox)
    boxes._TRANSPORTS["docker"] = UnixTransport   # WP1: adopt officially


install()
