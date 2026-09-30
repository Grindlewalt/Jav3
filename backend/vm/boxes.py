"""Boxes: the guests Jav3 runs, one record per box (DESIGN-BOXES.md, WP1).

A *box* is one disposable guest with its own identity on every channel the host
polices: its own CID (the gateway's key for "who is calling"), its own tap and
/30 (the nft key for "who is sending"), its own proxy listener (the egress
proxy's key for "whose traffic is this") and its own directory of runtime files.
Before boxes there was one guest, the `GuestVM` singleton in lifecycle.py; it
is now the **shared** box, and `lifecycle.vm` still names it, so every existing
caller keeps working unchanged.

Kinds (they decide what a box may do, and the gateway enforces it by CID):

    shared   the one turn guest every project uses unless its profile says
             otherwise. CID settings.vm_guest_cid (3), tap jvtap0, 10.201.0.1/.2.
    project  a project's own turn box (profile `separate_box`). May model_call
             and tool_broker_call, like the shared box, for ITS project only.
    service  runs approved services (WP3). No model, no broker, no op tokens:
             it may fetch its package and report status, nothing else.
    builder  builds an image-variant layer (WP5). Same limits as service.

Runtimes (`Box.runtime`, operator choice per profile, `box_runtime`):

    kvm      a QEMU/KVM guest (lifecycle.GuestVM). The host<->guest channel is
             AF_VSOCK: the guest dials CID 2 : settings.vm_vsock_port, the host
             dials the box CID on 5556 (run-turn), 5557 (shell), 5558 (svcd).
    docker   a hardened container (WP8). The SAME protocol runs over per-box
             AF_UNIX sockets in <vm_dir>/sock/<cid>/, bind-mounted into the
             container at /run/jav3. No TCP, never the docker socket.

The transport seam (`Transport`) is the only place the two differ for the
protocol: the gateway, guest_turn and the guest's server.py speak identical
NDJSON over whatever socket it hands them.

Addressing is derived from the box's slot number `cid` so the three identities
can never disagree: tap `jvtap<cid>`, subnet 10.201.<cid>.0/30, host .1, guest
.2, MAC 52:54:00:c9:00:<cid>. For kvm boxes `cid` is also the vsock CID; for
docker boxes it is only the slot (the bridge is `jvbr<cid>`).

Everything here is inert unless settings.vm_boxes_enabled: `for_project`
returns the shared box, nothing is allocated, and no new network exists.

The contract other packages code against is docs/boxes-contract.md.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import settings

KINDS = ("shared", "project", "service", "builder")
RUNTIMES = ("kvm", "docker")
PLACEMENTS = ("per_service", "per_project", "shared")

# which gateway ops each kind may use (the gateway enforces this by peer CID /
# by the per-box listener a unix connection arrived on). `get_guest_package`
# is answered with the package for the CALLER's kind, never a chosen one.
# service/builder report through ops WP3/WP5 define; they are listed here so
# the gate is in one place: svc_report (svcd status), build_report (builder).
GATEWAY_OPS: dict[str, frozenset[str]] = {
    "shared": frozenset({"ping", "get_guest_package", "model_call",
                         "tool_broker_call", "taint_note"}),
    "project": frozenset({"ping", "get_guest_package", "model_call",
                          "tool_broker_call", "taint_note"}),
    "service": frozenset({"ping", "get_guest_package", "svc_report"}),
    "builder": frozenset({"ping", "get_guest_package", "build_report"}),
}

# host -> guest service ports (the same numbers name the unix sockets)
PORT_RUNTURN = 5556
PORT_SHELL = 5557
PORT_SVCD = 5558

SHARED_ID = "shared"
SHARED_MAC = "52:54:00:12:34:60"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_VARIANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class BoxError(Exception):
    """A box request that cannot be met (bad kind, bad slug, unknown box)."""


class BoxCapError(BoxError):
    """Over vm_max_boxes / vm_max_project_boxes / vm_guest_ram_budget_mb, or
    the kind's CID range is exhausted. Refused, never squeezed in."""


# --- transports -------------------------------------------------------------

class Transport:
    """How the host and one box's guest reach each other. Two directions:

    guest -> host: the gateway (model_call, tool_broker_call, get_guest_package,
      ...). `gateway_endpoint()` tells the GUEST where to dial (it goes into
      box.json); the host side listens once for vsock (identity = peer CID) or
      once PER BOX for unix (identity = which listener accepted).
    host -> guest: run-turn (5556), shell (5557), svcd (5558). `connect(port)`
      returns a connected, non-blocking socket.

    Both carry the same newline-delimited JSON; nothing above this class
    knows which one it has."""

    name = "abstract"

    def __init__(self, box: "Box"):
        self.box = box

    async def connect(self, port: int) -> socket.socket:
        raise NotImplementedError

    def gateway_endpoint(self) -> dict:
        raise NotImplementedError

    def guest_listen(self, port: int) -> dict:
        raise NotImplementedError


class VsockTransport(Transport):
    name = "vsock"

    async def connect(self, port: int) -> socket.socket:
        loop = asyncio.get_running_loop()
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        try:
            # blocking connect in an executor: uvloop's sock_connect runs
            # getaddrinfo on the address and chokes on a (cid, port) tuple
            await loop.run_in_executor(None, s.connect, (self.box.cid, port))
        except BaseException:
            s.close()
            raise
        s.setblocking(False)
        return s

    def gateway_endpoint(self) -> dict:
        return {"transport": "vsock", "cid": 2, "port": settings.vm_vsock_port}

    def guest_listen(self, port: int) -> dict:
        return {"transport": "vsock", "port": port}


class UnixTransport(Transport):
    """Per-box AF_UNIX sockets in <vm_dir>/sock/<cid> (host view) == /run/jav3
    (container view). gateway.sock is the host's listener; <port>.sock are the
    guest's. The directory is the ONLY host path mounted into the container."""
    name = "unix"
    GUEST_DIR = "/run/jav3"

    @property
    def host_dir(self) -> Path:
        # short on purpose: sun_path is 108 bytes, and a long slug under
        # <vm_dir>/boxes/<id>/ would not fit
        return settings.vm_dir / "sock" / str(self.box.cid)

    def host_path(self, port: int | str) -> Path:
        return self.host_dir / f"{port}.sock"

    def gateway_path(self) -> Path:
        return self.host_dir / "gateway.sock"

    async def connect(self, port: int) -> socket.socket:
        loop = asyncio.get_running_loop()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            await loop.run_in_executor(None, s.connect, str(self.host_path(port)))
        except BaseException:
            s.close()
            raise
        s.setblocking(False)
        return s

    def gateway_endpoint(self) -> dict:
        return {"transport": "unix", "path": f"{self.GUEST_DIR}/gateway.sock"}

    def guest_listen(self, port: int) -> dict:
        return {"transport": "unix", "path": f"{self.GUEST_DIR}/{port}.sock"}


_TRANSPORTS: dict[str, type[Transport]] = {"kvm": VsockTransport,
                                           "docker": UnixTransport}


# --- the record -------------------------------------------------------------

def host_ram_mb() -> int | None:
    """This machine's physical RAM (the /vms budget bar used a hardcoded 4 GB,
    right only on the Pi). None where it can't be read."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        return None

@dataclass
class Box:
    """One box. Addressing fields are fixed at allocation and never change
    while the record lives; `ctl` is the runtime controller (GuestVM for kvm,
    WP8's driver for docker), created on first use."""
    id: str
    kind: str
    project: str | None
    cid: int
    tap: str
    host_ip: str
    guest_ip: str
    prefix: int
    mac: str
    image: tuple[str, str | None]          # (variant, version or None = active)
    mem_mb: int
    cpus: int
    dir: Path
    runtime: str = "kvm"
    service_id: int | None = None
    placement: str | None = None           # service boxes: per_service|per_project|shared
    allocated_at: float = field(default_factory=time.time)
    ctl: Any = field(default=None, repr=False, compare=False)
    # (variant, version) the running guest booted on; None until a boot
    booted_image: tuple[str, int | None] | None = field(default=None, compare=False)
    # project boxes: the OTHER projects that have run a turn here (placement
    # "join"). Kept for the box's life: once non-empty the egress proxy
    # attributes this box by its bound turn, like the shared box, never by
    # `project` alone (placement.py, "Join").
    joined: set = field(default_factory=set, compare=False)

    @property
    def transport(self) -> Transport:
        if self.runtime == "docker":
            _load_runtime("docker")     # installs the checked UnixTransport
        return _TRANSPORTS[self.runtime](self)

    @property
    def is_shared(self) -> bool:
        return self.kind == "shared"

    def may(self, op: str) -> bool:
        """Whether this box's kind may use gateway op `op`."""
        return op in GATEWAY_OPS.get(self.kind, frozenset())

    def box_json(self) -> dict:
        """The host-authored identity the guest reads instead of literals
        (shipped inside the kind's guest package as /opt/jarvis/box.json).
        The guest trusts nothing else about who it is; the host trusts nothing
        the guest says about it (identity is the CID / listener, not this)."""
        t = self.transport
        proxy = f"http://{self.host_ip}:{settings.vm_egress_proxy_port}"
        net = {"guest_ip": self.guest_ip, "prefix": self.prefix,
               "gateway": self.host_ip, "dns": self.host_ip,
               "proxy": proxy, "mac": self.mac}
        if self.runtime == "docker":
            # --network none (docs/docker-runtime.md 1): the only way out is the
            # in-container forwarder on the container's own loopback, spliced to
            # /run/jav3/proxy.sock. guest_ip/gateway/dns name nothing there.
            net["proxy"] = f"http://127.0.0.1:{settings.vm_egress_proxy_port}"
            net["proxy_socket"] = f"{UnixTransport.GUEST_DIR}/proxy.sock"
        return {"v": 1, "id": self.id, "kind": self.kind, "project": self.project,
                "runtime": self.runtime,
                "net": net,
                "gateway": t.gateway_endpoint(),
                "listen": {"runturn": t.guest_listen(PORT_RUNTURN),
                           "shell": t.guest_listen(PORT_SHELL),
                           "svcd": t.guest_listen(PORT_SVCD)}}

    def to_json(self) -> dict:
        """The static half of the /api/vm/boxes row (see status_json)."""
        return {"id": self.id, "kind": self.kind, "project": self.project,
                "cid": self.cid, "runtime": self.runtime,
                "service_id": self.service_id, "placement": self.placement,
                "image": {"variant": self.image[0], "version": self.image[1]},
                "mem_mb": self.mem_mb, "joined": sorted(self.joined),
                "net": {"tap": self.tap, "host_ip": self.host_ip,
                        "guest_ip": self.guest_ip}}


def ram_cost(mem_mb: int, runtime: str = "kvm") -> int:
    """What a box really costs the host: guest RAM plus, for a KVM box, the
    measured QEMU + firmware overhead (two 64 MB pflash images and QEMU itself:
    a `-m 384` box ran at 528 MB RSS on the Pi, e2e BUG-12). Pure."""
    return int(mem_mb) + (settings.vm_kvm_box_overhead_mb if runtime == "kvm" else 0)


def addressing(cid: int) -> dict:
    """tap / host_ip / guest_ip / prefix / mac for a non-shared slot. Pure."""
    if not 4 <= cid <= 254:
        raise BoxError(f"slot {cid} out of range (4..254)")
    return {"tap": f"jvtap{cid}", "host_ip": f"10.201.{cid}.1",
            "guest_ip": f"10.201.{cid}.2", "prefix": 30,
            "mac": f"52:54:00:c9:00:{cid:02x}"}


def _shared_box() -> Box:
    host = settings.vm_egress_host_ip
    guest = str(ipaddress.IPv4Address(host) + 1)      # 10.201.0.1 -> .2
    return Box(id=SHARED_ID, kind="shared", project=None,
               cid=settings.vm_guest_cid, tap=settings.vm_egress_tap,
               host_ip=host, guest_ip=guest, prefix=24, mac=SHARED_MAC,
               image=("main", None), mem_mb=settings.vm_memory_mb,
               cpus=settings.vm_cpus, dir=settings.vm_dir, runtime="kvm")


def box_id_for(kind: str, *, project: str | None = None,
               service_id: int | None = None, placement: str | None = None,
               variant: str | None = None, cid: int | None = None) -> str:
    """The stable id a box of this shape gets. Pure.

    project            p-<slug>
    service per_project s-<slug>        per_service s-<slug>-<service_id>
    service shared     s-shared
    builder            b-<variant>-<cid>"""
    if kind == "shared":
        return SHARED_ID
    if kind == "project":
        return f"p-{_slug(project)}"
    if kind == "service":
        placement = placement or "per_project"
        if placement not in PLACEMENTS:
            raise BoxError(f"unknown placement {placement!r}")
        if placement == "shared":
            return "s-shared"
        if placement == "per_service":
            if not isinstance(service_id, int):
                raise BoxError("per_service placement needs a service_id")
            return f"s-{_slug(project)}-{service_id}"
        return f"s-{_slug(project)}"
    if kind == "builder":
        return f"b-{_variant(variant)}-{cid}"
    raise BoxError(f"unknown kind {kind!r}")


def _slug(slug: str | None) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise BoxError(f"bad project slug {slug!r}")
    return slug


def _variant(v: str | None) -> str:
    v = v or "main"
    if not _VARIANT_RE.match(v):
        raise BoxError(f"bad image variant {v!r}")
    return v


def _cid_range(kind: str) -> range:
    lo, hi = {"project": (settings.vm_cid_project_min, settings.vm_cid_project_max),
              "service": (settings.vm_cid_service_min, settings.vm_cid_service_max),
              "builder": (settings.vm_cid_builder_min, settings.vm_cid_builder_max),
              }[kind]
    return range(max(lo, 4), min(hi, 254) + 1)


def _default_mem(kind: str) -> int:
    return {"project": settings.vm_project_box_mem_mb,
            "service": settings.vm_service_box_mem_mb,
            "builder": settings.vm_builder_box_mem_mb}[kind]


# --- the registry -----------------------------------------------------------

Hook = Callable[[str, Box], Awaitable[None]]


class Registry:
    """Allocation (CID, tap, subnet, directory) and lookup. Allocation is a
    RESERVATION: it counts against the caps whether or not the box is running,
    so a box that is allowed to exist can always boot. Single event loop, no
    awaits inside allocate/release: no lock needed."""

    def __init__(self):
        self._boxes: dict[str, Box] = {}
        self._op_box: dict[str, str] = {}
        self._op_project: dict[str, str | None] = {}

    # lookup ---------------------------------------------------------------
    def shared(self) -> Box:
        """Built fresh from settings on every call: its fields are today's
        settings, so nothing here can drift from what lifecycle.vm uses."""
        return _shared_box()

    def all(self) -> list[Box]:
        return [self.shared(), *sorted(self._boxes.values(), key=lambda b: b.cid)]

    def get(self, box_id: str) -> Box | None:
        if box_id == SHARED_ID:
            return self.shared()
        return self._boxes.get(box_id)

    def by_cid(self, cid: int | None) -> Box | None:
        """The kvm box whose vsock CID this is (the gateway's peer identity).
        Docker boxes are never returned: their slot is not a vsock CID."""
        for b in self.all():
            if b.cid == cid and b.runtime == "kvm":
                return b
        return None

    def by_host_ip(self, ip: str | None) -> Box | None:
        """The box whose host-side address this is (a proxy listener's box)."""
        for b in self.all():
            if b.host_ip == ip:
                return b
        return None

    def by_guest_ip(self, ip: str | None) -> Box | None:
        for b in self.all():
            if b.guest_ip == ip:
                return b
        return None

    def by_ifname(self, name: str | None) -> Box | None:
        for b in self.all():
            if b.tap == name:
                return b
        return None

    # caps ------------------------------------------------------------------
    def budget(self) -> dict:
        boxes = self.all()
        return {"ram_mb_used": sum(ram_cost(b.mem_mb, b.runtime) for b in boxes),
                "ram_mb_cap": settings.vm_guest_ram_budget_mb,
                "ram_mb_overhead_per_kvm_box": settings.vm_kvm_box_overhead_mb,
                "boxes": len(boxes), "boxes_cap": settings.vm_max_boxes,
                "project_boxes": sum(b.kind == "project" for b in boxes),
                "project_boxes_cap": settings.vm_max_project_boxes,
                "host_ram_mb": host_ram_mb()}

    def _check_caps(self, kind: str, mem_mb: int, runtime: str = "kvm") -> None:
        mem_mb = ram_cost(mem_mb, runtime)
        b = self.budget()
        if b["boxes"] + 1 > settings.vm_max_boxes:
            raise BoxCapError(f"box cap reached ({b['boxes']}/{settings.vm_max_boxes})")
        if kind == "project" and b["project_boxes"] + 1 > settings.vm_max_project_boxes:
            raise BoxCapError("project box cap reached "
                              f"({b['project_boxes']}/{settings.vm_max_project_boxes})")
        if b["ram_mb_used"] + mem_mb > settings.vm_guest_ram_budget_mb:
            raise BoxCapError(
                f"guest RAM budget: {b['ram_mb_used']} + {mem_mb} MB > "
                f"{settings.vm_guest_ram_budget_mb} MB")

    # allocate / release ------------------------------------------------------
    def allocate(self, kind: str, *, project: str | None = None,
                 service_id: int | None = None, placement: str | None = None,
                 variant: str | None = None, version: str | None = None,
                 mem_mb: int | None = None, runtime: str = "kvm") -> Box:
        """Reserve a box. Idempotent: the same shape returns the existing box.
        Raises BoxError for a bad request and BoxCapError over a cap. Never
        called for 'shared' (it always exists)."""
        if kind not in KINDS or kind == "shared":
            raise BoxError(f"cannot allocate kind {kind!r}")
        if runtime not in RUNTIMES:
            raise BoxError(f"unknown runtime {runtime!r}")
        if runtime == "docker" and not settings.docker_enabled:
            raise BoxError("docker runtime is disabled (docker_enabled)")
        variant = _variant(variant)
        if kind in ("project",) or (kind == "service" and placement != "shared"):
            _slug(project)
        if kind != "builder":
            existing = self._boxes.get(box_id_for(
                kind, project=project, service_id=service_id, placement=placement,
                variant=variant))
            if existing is not None:
                return existing
        mem = int(mem_mb or _default_mem(kind))
        if kind != "builder" and runtime == "kvm":
            mem = max(mem, mem_floor(variant))
        if runtime == "docker":
            mem = int(mem_mb or settings.docker_box_mem_mb)
        self._check_caps(kind, mem, runtime)
        used = {b.cid for b in self.all()}
        cid = next((c for c in _cid_range(kind) if c not in used), None)
        if cid is None:
            raise BoxCapError(f"no free {kind} slot")
        bid = box_id_for(kind, project=project, service_id=service_id,
                         placement=placement, variant=variant, cid=cid)
        net = addressing(cid)
        if runtime == "docker":
            net["tap"] = f"jvbr{cid}"
        cpus = {"service": settings.vm_service_box_cpus}.get(kind, settings.vm_box_cpus)
        box = Box(id=bid, kind=kind,
                  project=None if (kind == "service" and placement == "shared")
                  or kind == "builder" else project,
                  cid=cid, image=(variant, version), mem_mb=mem, cpus=cpus,
                  dir=settings.vm_dir / "boxes" / bid, runtime=runtime,
                  service_id=service_id if kind == "service" else None,
                  placement=(placement or "per_project") if kind == "service" else None,
                  **net)
        self._boxes[bid] = box
        return box

    def release(self, box_id: str) -> Box | None:
        """Drop a reservation. The caller stops the box first (stop()); the
        shared box cannot be released."""
        if box_id == SHARED_ID:
            raise BoxError("the shared box is never released")
        box = self._boxes.pop(box_id, None)
        if box is not None:
            for op, bid in list(self._op_box.items()):
                if bid == box_id:
                    self._op_box.pop(op, None)
                    self._op_project.pop(op, None)
        return box

    # op binding --------------------------------------------------------------
    def bind_op(self, op_id: str, box: Box, project: str | None = None) -> None:
        """Record that `op_id`'s turn runs in `box` (set by guest_turn). The
        gateway refuses an op that arrives from a different box. `project` is
        the turn's project: a joined box runs one project's turns at a time
        and a project's turns stay in the box it is running in."""
        self._op_box[op_id] = box.id
        self._op_project[op_id] = project

    def unbind_op(self, op_id: str) -> None:
        self._op_box.pop(op_id, None)
        self._op_project.pop(op_id, None)

    def live_box(self, project: str) -> Box | None:
        """The box a turn of `project` is bound to right now, or None."""
        for op, bid in reversed(list(self._op_box.items())):
            if self._op_project.get(op) == project:
                b = self.get(bid)
                if b is not None:
                    return b
        return None

    def other_projects(self, box: Box, project: str | None) -> set:
        """Projects other than `project` with a turn bound to `box`."""
        return {self._op_project.get(op) for op, bid in self._op_box.items()
                if bid == box.id} - {project}

    def op_box(self, op_id: str) -> str | None:
        return self._op_box.get(op_id)

    def reset(self) -> None:
        """Tests only: forget everything (settings may have changed)."""
        self._boxes.clear()
        self._op_box.clear()
        self._op_project.clear()


registry = Registry()

# module-level API (the names other packages import)
shared = registry.shared
get = registry.get
by_cid = registry.by_cid
by_host_ip = registry.by_host_ip
by_guest_ip = registry.by_guest_ip
by_ifname = registry.by_ifname
allocate = registry.allocate
release = registry.release
budget = registry.budget
bind_op = registry.bind_op
unbind_op = registry.unbind_op
op_box = registry.op_box
live_box = registry.live_box


def all_boxes() -> list[Box]:
    return registry.all()


def enabled() -> bool:
    return bool(settings.vm_boxes_enabled)


# --- profile resolution -------------------------------------------------------

async def project_profile(slug: str) -> dict | None:
    """The security profile row for a project (projects.profile_id, falling
    back to the one marked is_default, created on first use if none exists)."""
    from ..db import get_db
    db = await get_db()
    try:
        async with db.execute(
                "SELECT sp.* FROM projects p JOIN security_profiles sp "
                "ON sp.id = p.profile_id WHERE p.slug = ?", (slug,)) as cur:
            row = await cur.fetchone()
        if row is None:
            # the marked default; the first use creates it when setup did not
            from .. import profiles
            p = await profiles.default(db)
            async with db.execute("SELECT * FROM security_profiles WHERE id = ?",
                                  (p["id"],)) as cur:
                row = await cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        await db.close()


def image_version(variant: str, pinned=None) -> int | None:
    """The version a box of `variant` boots on now: the pinned one, else the
    variant's active built version (None for a variant with no built layer,
    e.g. main on the base)."""
    if pinned is not None and str(pinned).lstrip("v").isdigit():
        return int(str(pinned).lstrip("v"))
    try:
        from . import images
        return images.active_version(variant)
    except Exception:  # noqa: BLE001 - a status field, never a failure
        return None


def follow_profile_image(box: Box, variant: str | None) -> bool:
    """A project box follows its profile's `box_image` (e2e BUG-11: a changed
    image was ignored until the box was destroyed). A STOPPED box switches in
    place (its overlay is rebuilt at every boot anyway); a running one keeps
    its image until it stops, and its row says restart_needed. A variant whose
    RAM floor the box does not meet needs a destroy (the budget decides).
    Returns True when the box now has that variant."""
    variant = variant or "main"
    if box.kind != "project" or box.image[0] == variant:
        return True
    ctl = box.ctl
    if ctl is not None and ctl.running():
        return False
    if box.runtime == "kvm" and mem_floor(variant) > box.mem_mb:
        return False
    box.image = (variant, None)
    return True


async def for_project(slug: str | None) -> Box:
    """The box a turn of project `slug` runs in (placement.py).

    Flag off or no slug: the shared box. A project with a turn bound to a box
    right now keeps using it (nested and concurrent turns reuse that box's
    workspace copy; a changed placement applies from the next turn after).
    Otherwise its placement: its own setting, else its profile's default.

    shared   the shared box. (Profile default only: an existing p-<slug> the
             operator warmed up is still used, as before placements.)
    own      p-<slug>, allocated on first use with the placement's runtime /
             image / memory. Caps raise BoxCapError after idle project boxes
             have given way: a turn never silently falls back to a weaker
             boundary than it was given.
    join     another project's box (Box.joined records it for attribution);
             re-created from its owner's placement if it was reaped, refused
             (BoxError) if the owner no longer runs in a box of its own."""
    if not settings.vm_boxes_enabled or not slug:
        return registry.shared()
    live = registry.live_box(slug)
    if live is not None:
        return live
    from . import placement
    eff = await placement.effective(slug)
    if eff["mode"] == "join":
        return await _join_box(slug, eff["box_id"])
    existing = registry.get(f"p-{slug}") if _SLUG_RE.match(slug) else None
    if eff["mode"] == "shared":
        if existing is not None and eff["source"] == "profile":
            return existing
        return registry.shared()
    return await _own_box(slug, eff)


def _bound(box: Box) -> bool:
    return any(bid == box.id for bid in registry._op_box.values())


async def _own_box(slug: str, eff: dict) -> Box:
    existing = registry.get(f"p-{slug}")
    if existing is not None:
        ctl = existing.ctl
        if (existing.runtime != (eff.get("runtime") or "kvm") and not _bound(existing)
                and not (ctl is not None and ctl.running())):
            # a stopped, idle box on the old runtime: disposable, re-made below
            from . import boxlog
            async with boxlog.action(existing, "destroyed", reason=(
                    f"runtime changed to {eff.get('runtime') or 'kvm'}")):
                await destroy(existing)
        else:
            follow_profile_image(existing, eff.get("image"))
            return existing
    # an idle project box holding the only slot (or the RAM) gives way to a
    # turn that needs one: it is disposable, exactly what the idle reaper
    # would do a few minutes later
    for _ in range(settings.vm_max_boxes + 1):
        try:
            return registry.allocate(
                "project", project=slug, variant=eff.get("image") or "main",
                mem_mb=eff.get("mem_mb"), runtime=eff.get("runtime") or "kvm")
        except BoxCapError:
            victim = idle_project_box(exclude=f"p-{slug}")
            if victim is None:
                raise
            from . import boxlog
            async with boxlog.action(victim, "destroyed",
                                     reason=f"idle, gave way to p-{slug} (box cap)"):
                await destroy(victim)
    raise BoxCapError("no box could be freed")


async def _join_box(slug: str, box_id: str) -> Box:
    from . import placement
    owner = placement.owner_of(box_id)
    box = registry.get(box_id)
    if box is None and owner:
        o = await placement.effective(owner)
        if o["mode"] != "own":
            raise BoxError(f"{slug} is set to run in {box_id}, which is gone, and "
                           f"{owner} no longer runs in a box of its own: pick "
                           f"another box for {slug} (Runs in)")
        box = await _own_box(owner, o)
    if box is None or box.kind != "project":
        raise BoxError(f"{slug} cannot join {box_id}: not a project's turn box")
    box.joined.add(slug)
    return box


JOIN_WAIT_SECONDS = 600.0


async def wait_turn_slot(box: Box, slug: str | None) -> Box:
    """A joined box runs one project's turns at a time, so its proxy can
    attribute traffic to the one project whose turn is live. Waits while a
    turn of another project is bound to `box`; refuses (BoxError) after
    JOIN_WAIT_SECONDS. Returns the box to use (re-resolved if it vanished
    while waiting). The caller binds its op with NO await after this."""
    if box.is_shared or not slug or not box.joined:
        return box
    deadline = time.monotonic() + JOIN_WAIT_SECONDS
    while True:
        if registry.get(box.id) is not box:
            box = await for_project(slug)
            if box.is_shared or not box.joined:
                return box
        others = registry.other_projects(box, slug)
        if not others:
            return box
        if time.monotonic() >= deadline:
            raise BoxError(
                f"{box.id} is running a turn of {', '.join(sorted(map(str, others)))}; "
                "a joined box runs one project's turns at a time "
                f"(waited {int(JOIN_WAIT_SECONDS)} s)")
        await asyncio.sleep(0.2)


def idle_project_box(exclude: str | None = None) -> Box | None:
    """The longest-idle project box with no turn bound or in flight, or None."""
    bound = set(registry._op_box.values())
    cands = [b for b in registry.all()
             if b.kind == "project" and b.id != exclude and b.id not in bound
             and (b.ctl is None or getattr(b.ctl, "inflight", 0) == 0)]
    cands.sort(key=lambda b: getattr(b.ctl, "idle_since", None) or 0.0)
    return cands[0] if cands else None


# --- runtime drivers + hooks ----------------------------------------------------

# runtime -> factory(box) -> controller. The controller interface is GuestVM's:
#   running() -> bool; pid -> int | None; booted_at -> float | None;
#   inflight -> int; async acquire(); release(); async boot(); async teardown()
_DRIVERS: dict[str, Callable[[Box], Any]] = {}
_hooks: list[Hook] = []
_image_resolvers: list[Callable[[Box], Path | None]] = []
_data_deleters: list[Callable[[Box], Awaitable[None]]] = []

BUS_CHAN = "vm-boxes"


_mem_floors: list[Callable[[str], int | None]] = []


def add_mem_floor(fn: Callable[[str], int | None]) -> None:
    """WP5: fn(variant) -> the variant's minimum guest RAM in MB, or None.
    allocate() raises a KVM box's memory to the highest floor (builders are
    exempt: they install, they do not run the variant's workload)."""
    if fn not in _mem_floors:
        _mem_floors.append(fn)


def mem_floor(variant: str) -> int:
    """The floor for `variant`: the registered floors (images.min_mem_mb reads
    the recipe's min_mem_mb, inherited by variants built from desktop), and
    the vm_desktop_min_mem_mb setting for 'desktop' whatever the recipe says."""
    floor = settings.vm_desktop_min_mem_mb if variant == "desktop" else 0
    for fn in list(_mem_floors):
        try:
            v = fn(variant)
        except Exception:  # noqa: BLE001 — a broken floor must not block allocation
            v = None
        if isinstance(v, int) and v > floor:
            floor = v
    return floor


_RUNTIME_MODULES = {"kvm": ".lifecycle", "docker": ".docker_runtime"}


def _load_runtime(name: str) -> None:
    """Import a runtime's driver on first use: importing registers it (and the
    docker driver installs its checked transport_unix.UnixTransport). The
    docker module is never imported unless a docker box is touched, so with
    docker_enabled off nothing about the process changes."""
    if name in _DRIVERS:
        return
    mod = _RUNTIME_MODULES.get(name)
    if mod is not None:
        import importlib
        m = importlib.import_module(mod, __package__)
        install = getattr(m, "install", None)     # idempotent re-registration
        if name not in _DRIVERS and callable(install):
            install()


def register_runtime(name: str, factory: Callable[[Box], Any]) -> None:
    """WP8 registers 'docker' here. lifecycle registers 'kvm' on import."""
    _DRIVERS[name] = factory


def add_hook(fn: Hook) -> None:
    """`await fn(event, box)` for event in box_up (network ready, BEFORE the
    guest boots) and box_down (after it is gone). The egress proxy (WP2)
    starts/stops the box's listener here; a hook that raises on box_up fails
    the start (fail closed)."""
    _hooks.append(fn)


def add_image_resolver(fn: Callable[[Box], Path | None]) -> None:
    """WP5: map a box's (variant, version) to the qcow2 its overlay backs on.
    First non-None wins; the builtin resolver answers 'main' with the active
    base-vN.qcow2 and refuses every other variant."""
    _image_resolvers.append(fn)


def add_data_deleter(fn: Callable[[Box], Awaitable[None]]) -> None:
    """WP3: destroy(delete_data=True) calls these (the /srv disk)."""
    _data_deleters.append(fn)


def image_path(box: Box) -> Path:
    if box.image[0] != "main" and not _image_resolvers:
        # WP5's resolver (variants: svc, dev, desktop, operator ones) registers
        # on import; a service box must never fail for lack of that import
        import importlib
        importlib.import_module(".images", __package__)
    for fn in _image_resolvers:
        p = fn(box)
        if p is not None:
            return p
    if box.image[0] != "main":
        raise BoxError(f"no image for variant {box.image[0]!r} (not built)")
    from .lifecycle import _base_image
    return _base_image()


def controller(box: Box):
    """The box's runtime controller. The shared box's is ALWAYS the current
    `lifecycle.vm` (never cached: callers and tests that swap it are obeyed)."""
    if box.is_shared:
        from . import lifecycle
        return lifecycle.vm
    if box.ctl is None:
        _load_runtime(box.runtime)
        if box.runtime not in _DRIVERS:
            raise BoxError(f"runtime {box.runtime!r} is not available")
        box.ctl = _DRIVERS[box.runtime](box)
    return box.ctl


async def _emit(event: str, box: Box) -> None:
    from .. import bus
    from . import boxlog
    for fn in list(_hooks):
        await fn(event, box)
    bus.publish(BUS_CHAN, {"type": event, "box": box.to_json()})
    await boxlog.on_emit(event, box)       # the box's history (never raises)


async def box_up(box: Box) -> None:
    await _emit("box_up", box)


async def box_down(box: Box) -> None:
    try:
        await _emit("box_down", box)
    except Exception:  # noqa: BLE001 — teardown must finish whatever a hook does
        pass


async def start(box: Box) -> None:
    """Boot the box (network, hooks, guest) and wait until it serves."""
    ctl = controller(box)
    await ctl.acquire()
    ctl.release()


async def stop(box: Box) -> None:
    """Kill the guest. Disposable: the next start is a fresh overlay."""
    ctl = controller(box)
    await ctl.teardown()


async def destroy(box: Box, *, delete_data: bool = False,
                  reason: str | None = None) -> None:
    """Stop, release the reservation, delete its directory (and, on request,
    its data via the WP3 deleters). The shared box can only be stopped.
    `reason` goes into the box's history (boxlog)."""
    import shutil
    from . import boxlog
    if box.is_shared:
        await stop(box)
        return
    why = " ".join(x for x in (reason, "(data deleted)" if delete_data else None) if x)
    async with boxlog.action(box, "destroyed", reason=why or None):
        await stop(box)
        if delete_data:
            for fn in list(_data_deleters):
                await fn(box)
        registry.release(box.id)
        forget = getattr(box.ctl, "forget", None)
        if forget is not None:              # a docker box's socket directory
            await forget()
        shutil.rmtree(box.dir, ignore_errors=True)


async def restart(box: Box) -> None:
    """Stop and boot again: a fresh overlay (KVM) or a fresh container."""
    from . import boxlog
    async with boxlog.action(box, "restarted"):
        await stop(box)
        await start(box)


async def stop_all() -> None:
    """App shutdown: stop every non-shared box (the shared one is lifecycle.vm's)."""
    for box in list(registry.all()):
        if not box.is_shared and box.ctl is not None:
            try:
                await box.ctl.teardown()
            except Exception:  # noqa: BLE001 — shutdown stops the rest regardless
                pass


async def reap_idle() -> None:
    """Stop + release project boxes idle past vm_box_idle_stop_seconds (the
    same as a scrub: they are disposable). A box that never came up counts
    too: one whose boot failed (its docker controller starts the clock at the
    failure) or that was never booted at all (counted from its allocation),
    which would otherwise hold a slot and its RAM reservation until someone
    destroyed it. Service/builder boxes are managed by their owners (WP3/WP5)."""
    from . import boxlog
    window = settings.vm_box_idle_stop_seconds
    if not settings.vm_boxes_enabled or not window:
        return
    now = time.monotonic()
    for box in list(registry.all()):
        if box.kind != "project":
            continue
        ctl = box.ctl
        if ctl is not None and ctl.inflight:
            continue
        idle = getattr(ctl, "idle_since", None)
        if idle is None and not (ctl is not None and ctl.running()):
            idle = now - (time.time() - box.allocated_at)
        if idle is None or now - idle < window or _bound(box):
            continue
        if getattr(ctl, "state", None) == "failed":
            why = (f"boot failed {_mins(now - idle)} ago, released after "
                   f"{_mins(window)}: {getattr(ctl, 'error', None) or 'no reason recorded'}")
        else:
            why = f"idle {_mins(now - idle)} (stops at {_mins(window)})"
        async with boxlog.action(box, "idle_stopped", actor="reaper", reason=why):
            await destroy(box)


def _mins(s) -> str:
    """45s, 4m, 1h05m: the /vms screens' idle-timer words. Pure."""
    s = max(0, int(s or 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def _wall(mono: float | None) -> float | None:
    """A time.monotonic() stamp as wall-clock epoch seconds."""
    return None if mono is None else round(time.time() - (time.monotonic() - mono), 1)


def idle_timer(box: Box, ctl, running: bool) -> dict:
    """When the reaper acts on this box, from the controller's idle clock.
    project: stopped and released after vm_box_idle_stop_seconds idle (boxes
    on); shared: scrubbed (rebooted fresh) after vm_idle_scrub_seconds (on
    when > 0). Services and builders have no idle stop. `idle_s` / `stops_in_s`
    are server-computed, so a client's clock does not matter."""
    out = {"idle_since": None, "idle_s": None, "stop_action": None,
           "stop_after_s": None, "stops_at": None, "stops_in_s": None}
    if box.kind == "project" and settings.vm_boxes_enabled and settings.vm_box_idle_stop_seconds:
        out.update(stop_action="stop", stop_after_s=int(settings.vm_box_idle_stop_seconds))
    elif box.is_shared and settings.vm_idle_scrub_seconds:
        out.update(stop_action="scrub", stop_after_s=int(settings.vm_idle_scrub_seconds))
    idle = getattr(ctl, "idle_since", None) if ctl is not None else None
    if not running or idle is None or getattr(ctl, "inflight", 0):
        return out
    now = time.monotonic()
    out.update(idle_since=_wall(idle), idle_s=int(now - idle))
    if out["stop_after_s"]:
        left = max(0, out["stop_after_s"] - out["idle_s"])
        out.update(stops_in_s=left, stops_at=round(time.time() + left, 1))
    return out


def _proc_stats(pid: int | None) -> dict:
    """RSS (bytes) and cumulative CPU seconds of a QEMU pid from /proc."""
    if not pid:
        return {"rss_bytes": None, "cpu_s": None}
    try:
        with open(f"/proc/{pid}/statm") as f:
            rss = int(f.read().split()[1]) * 4096
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().rsplit(")", 1)[1].split()
        import os
        tck = os.sysconf("SC_CLK_TCK")
        cpu = (int(parts[11]) + int(parts[12])) / tck
        return {"rss_bytes": rss, "cpu_s": round(cpu, 2)}
    except (OSError, ValueError, IndexError):
        return {"rss_bytes": None, "cpu_s": None}


def _disk(box: Box) -> dict:
    def used(p: Path) -> int | None:
        try:
            return p.stat().st_blocks * 512
        except OSError:
            return None
    data = None
    if box.kind == "service" and box.project:
        data = used(settings.vm_dir / "svc" / box.project / "data.qcow2")
    return {"overlay_bytes": used(box.dir / "overlay.qcow2"), "data_bytes": data}


_cpu_prev: dict[str, tuple[float, float]] = {}


def status_json(box: Box) -> dict:
    """One /api/vm/boxes row (docs/boxes-contract.md, section E)."""
    ctl = controller(box) if box.is_shared else box.ctl
    running = bool(ctl and ctl.running())
    pid = getattr(ctl, "pid", None) if running else None
    st = _proc_stats(pid)
    cpu_pct = None
    if st["cpu_s"] is not None:
        now = time.monotonic()
        prev = _cpu_prev.get(box.id)
        _cpu_prev[box.id] = (now, st["cpu_s"])
        if prev and now > prev[0]:
            cpu_pct = round(100 * (st["cpu_s"] - prev[1]) / (now - prev[0]), 1)
    booted = getattr(ctl, "booted_at", None) if ctl else None
    row = box.to_json()
    # the version it runs (running) or would boot (stopped); e2e BUG-11 had
    # it null always
    target = (box.image[0], image_version(box.image[0], box.image[1]))
    now = box.booted_image if running and box.booted_image else target
    row["image"] = {"variant": now[0], "version": now[1]}
    inflight = getattr(ctl, "inflight", 0) if ctl else 0
    return {**row,
            "restart_needed": bool(running and box.booted_image
                                   and box.booted_image != target),
            "state": "running" if running else "stopped",
            "rss_bytes": st["rss_bytes"], "cpu_pct": cpu_pct,
            "uptime_s": int(time.monotonic() - booted) if running and booted else None,
            "inflight": inflight,
            "disk": _disk(box),
            # at a glance (the /vms screens): who it serves, what it costs,
            # what it is doing, when the reaper acts, what last went wrong
            "projects": ([box.project] if box.project else []) + sorted(
                box.joined - {box.project}),
            "ram_cost_mb": ram_cost(box.mem_mb, box.runtime),
            "started_at": _wall(booted) if running and booted else None,
            "activity": _activity(ctl, running, inflight),
            "last_error": _last_error(ctl),
            **idle_timer(box, ctl, running)}


def _activity(ctl, running: bool, inflight: int) -> str:
    """One word: starting | busy | idle | stopped | failed."""
    st = getattr(ctl, "state", None)          # the docker controller's state
    if st == "failed" and not running:
        return "failed"
    if not running:
        return "stopped"
    if st == "starting" or getattr(ctl, "starting", False):
        return "starting"
    return "busy" if inflight else "idle"


def _last_error(ctl) -> str | None:
    err = getattr(ctl, "error", None) if ctl is not None else None
    return str(err) if err else None
