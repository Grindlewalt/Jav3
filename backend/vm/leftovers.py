"""Leftovers: things a box left behind that no box of this server owns now.

An app restart forgets every box (the registry is in memory) while a QEMU
started with setsid, a container, a box directory or a socket directory can
outlive it. `scan()` lists them with why; `clean()` removes only what it can
positively identify as THIS server's:

    container  a docker container with Jav3's label (jav3.managed=1), named
               jav3-<its jav3.box label>, whose socket bind mount is under this
               server's <vm_dir>/sock. A container of another Jav3 install on
               the same daemon (its mount elsewhere) is not listed; anything
               without the label is never looked at. The operator runs
               portainer, jellyfin and the rest beside Jav3.
    qemu       a qemu-system-* process whose working directory is this
               server's vm_dir (the shared box) or <vm_dir>/boxes/<id>, that
               no box controller here started.
    box_dir    <vm_dir>/boxes/<id> with no box registered (overlay disk, EFI
               vars, console log: disposable by design, destroy deletes it).
    sock_dir   <vm_dir>/sock/<n> with no docker box in slot n.
    overlay    the shared box's overlay.qcow2 while it is stopped.
    tap        a jvtapN / jvbrN interface no box here uses. Listed, never
               removed: the names are not per-install, and removing one needs
               sudo (vm/net_up.sh).

The scan is cheap (one `docker ps` + one `docker inspect`, a /proc walk, two
directory listings) and cached SCAN_TTL seconds. The app runs it once, read
only, shortly after startup and logs the count (lifecycle.reaper_loop).
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import time
from pathlib import Path

from ..config import settings
from . import boxes

SCAN_TTL = 15.0
PROC = Path("/proc")
SYS_NET = Path("/sys/class/net")
_TAP_RE = re.compile(r"^jv(?:tap|br)(\d+)$")
_ORDER = {"qemu": 0, "container": 1, "overlay": 2, "box_dir": 3, "sock_dir": 4}
_cache: tuple[float, dict] | None = None


def _resolved(p) -> Path | None:
    try:
        return Path(p).resolve()
    except (OSError, RuntimeError):
        return None


def _under(p: Path | None, root: Path) -> bool:
    if p is None:
        return False
    r = _resolved(root)
    return r is not None and (p == r or r in p.parents)


def _du(p: Path) -> int | None:
    """Bytes actually used under p (files only, no symlinks followed)."""
    total = 0
    try:
        for q in [p] if p.is_file() else p.rglob("*"):
            if q.is_file() and not q.is_symlink():
                total += q.stat().st_blocks * 512
    except OSError:
        return None
    return total


def _controllers():
    """(box, controller) for every registered box, the shared one included."""
    for b in boxes.all_boxes():
        ctl = boxes.controller(b) if b.is_shared else b.ctl
        yield b, ctl


def _live_pids() -> set[int]:
    out = set()
    for _b, ctl in _controllers():
        pid = getattr(ctl, "pid", None) if ctl is not None else None
        if isinstance(pid, int) and pid > 0:
            out.add(pid)
    return out


# --- qemu -----------------------------------------------------------------------

def _qemu_procs() -> list[dict]:
    """QEMU processes whose cwd is this server's vm_dir or one of its box
    directories: {pid, cwd, box_id, cid}. Linux /proc; empty elsewhere."""
    vm_dir = _resolved(settings.vm_dir)
    boxes_dir = _resolved(settings.vm_dir / "boxes")
    out = []
    try:
        pids = [p for p in PROC.iterdir() if p.name.isdigit()]
    except OSError:
        return out
    for p in pids:
        try:
            argv = (p / "cmdline").read_bytes().split(b"\0")
            if not argv or not os.path.basename(argv[0].decode(errors="replace")
                                                ).startswith("qemu-system"):
                continue
            cwd = os.readlink(p / "cwd")
        except OSError:
            continue
        cwd = cwd.removesuffix(" (deleted)")
        c = _resolved(cwd) or Path(cwd)
        if c == vm_dir:
            bid = boxes.SHARED_ID
        elif boxes_dir is not None and c.parent == boxes_dir:
            bid = c.name
        else:
            continue
        cid = next((a.decode(errors="replace").split("guest-cid=", 1)[1].split(",")[0]
                    for a in argv if b"guest-cid=" in a), None)
        out.append({"pid": int(p.name), "cwd": str(c), "box_id": bid, "cid": cid})
    return out


# --- docker -----------------------------------------------------------------------

async def _containers() -> list[dict]:
    """This server's containers only (see the module doc): {name, box_id,
    status}. Docker off: none, and docker is never called."""
    if not settings.docker_enabled:
        return []
    from . import docker_runtime as dr
    rc, out, _ = await dr.cli.run("ps", "--all", "--filter", f"label={dr.LABEL}=1",
                                  "--format", "{{.Names}}", timeout=30)
    names = [n for n in out.split() if n.startswith("jav3-")] if rc == 0 else []
    if not names:
        return []
    fmt = ('{{.Name}}\t{{index .Config.Labels "' + dr.LABEL + '"}}\t'
           '{{index .Config.Labels "jav3.box"}}\t{{.State.Status}}\t{{json .Mounts}}')
    rc, out, _ = await dr.cli.run("inspect", "--format", fmt, *names, timeout=30)
    if rc != 0:
        return []
    import json
    sock_root = settings.vm_dir / "sock"
    found = []
    for line in out.splitlines():
        parts = line.split("\t", 4)
        if len(parts) != 5:
            continue
        name, managed, bid, status, mounts = parts
        name = name.lstrip("/")
        if managed != "1" or not bid or name != f"jav3-{bid}":
            continue
        try:
            ms = json.loads(mounts) or []
        except ValueError:
            continue
        if not any(isinstance(m, dict) and m.get("Type") == "bind"
                   and _under(_resolved(m.get("Source") or "/nonexistent"), sock_root)
                   for m in ms):
            continue                       # another Jav3 install's container
        found.append({"name": name, "box_id": bid, "status": status})
    return found


def _live_containers() -> set[str]:
    from . import docker_runtime as dr
    return {dr.container_name(b) for b, ctl in _controllers()
            if b.runtime == "docker" and ctl is not None and ctl.running()}


# --- the scan ------------------------------------------------------------------------

async def scan(*, cached: bool = False) -> dict:
    """{items: [{id, type, name, why, cleanable, bytes?, detail}], docker,
    scanned_at}. Nothing is changed."""
    global _cache
    now = time.monotonic()
    if cached and _cache and now - _cache[0] < SCAN_TTL:
        return _cache[1]
    items: list[dict] = []
    reg = {b.id: b for b in boxes.all_boxes()}
    live = _live_pids()
    qemus = _qemu_procs()
    for q in qemus:
        if q["pid"] in live:
            continue
        where = "the shared box's" if q["box_id"] == boxes.SHARED_ID else f"{q['box_id']}'s"
        items.append({"id": f"qemu:{q['pid']}", "type": "qemu",
                      "name": f"qemu pid {q['pid']}",
                      "why": f"QEMU running in {where} directory that no box here "
                             "started (left over from an app restart)",
                      "cleanable": True, "detail": q})
    # a directory a LIVE box's QEMU runs in is in use; one a leftover QEMU
    # runs in is a leftover too (clean kills the QEMU first)
    running_dirs = {q["cwd"] for q in qemus if q["pid"] in live}
    try:
        live_names = _live_containers() if settings.docker_enabled else set()
        conts = await _containers()
    except Exception as e:  # noqa: BLE001 — a dead daemon: say so, list the rest
        conts, docker = [], f"unavailable: {e}"
    else:
        docker = "on" if settings.docker_enabled else "off"
    for c in conts:
        if c["name"] in live_names:
            continue
        b = reg.get(c["box_id"])
        why = (f"its box {c['box_id']} is stopped" if b is not None
               else f"no box {c['box_id']} exists (left over from an app restart)")
        items.append({"id": f"container:{c['name']}", "type": "container",
                      "name": c["name"], "why": f"container {c['status']}: {why}",
                      "cleanable": True, "detail": c})
    bdir = settings.vm_dir / "boxes"
    try:
        dirs = sorted(p for p in bdir.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        dirs = []
    for d in dirs:
        if d.name in reg or str(_resolved(d)) in running_dirs:
            continue
        items.append({"id": f"box_dir:{d.name}", "type": "box_dir", "name": f"boxes/{d.name}",
                      "why": "box directory with no box registered (overlay disk and "
                             "runtime files; destroy would have deleted it)",
                      "cleanable": True, "bytes": _du(d), "detail": {"path": str(d)}})
    sdir = settings.vm_dir / "sock"
    slots = {str(b.cid) for b in reg.values() if b.runtime == "docker"}
    try:
        socks = sorted(p for p in sdir.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        socks = []
    for d in socks:
        if d.name in slots:
            continue
        items.append({"id": f"sock_dir:{d.name}", "type": "sock_dir", "name": f"sock/{d.name}",
                      "why": f"socket directory for slot {d.name}, which no docker box uses",
                      "cleanable": True, "detail": {"path": str(d)}})
    shared = reg.get(boxes.SHARED_ID)
    ov = settings.vm_dir / "overlay.qcow2"
    sctl = boxes.controller(shared) if shared is not None else None
    if (ov.exists() and sctl is not None and not sctl.running()
            and str(_resolved(settings.vm_dir)) not in running_dirs):
        items.append({"id": "overlay:shared", "type": "overlay", "name": "overlay.qcow2",
                      "why": "the shared box is stopped but its overlay disk is still "
                             "there (the next boot makes a new one)",
                      "cleanable": True, "bytes": _du(ov), "detail": {"path": str(ov)}})
    taps = {b.tap for b in reg.values()} | {settings.vm_egress_tap}
    try:
        nets = sorted(p.name for p in SYS_NET.iterdir())
    except OSError:
        nets = []
    for n in nets:
        if _TAP_RE.match(n) and n not in taps:
            items.append({"id": f"tap:{n}", "type": "tap", "name": n,
                          "why": "network interface no box here uses (another install "
                                 "may; removing it needs sudo vm/net_up.sh)",
                          "cleanable": False, "detail": {"ifname": n}})
    out = {"items": items, "docker": docker, "scanned_at": time.time(),
           "cleanable": sum(1 for i in items if i["cleanable"])}
    _cache = (now, out)
    return out


async def clean(ids: list[str] | None = None) -> dict:
    """Remove the cleanable leftovers (all, or those whose id is in `ids`),
    each re-identified by a fresh scan first. {removed: [id], failed:
    [{id, error}], skipped: [id]}."""
    global _cache
    _cache = None
    fresh = {i["id"]: i for i in (await scan())["items"]}
    want = list(fresh) if ids is None else [i for i in ids if isinstance(i, str)]
    # processes and containers before the directories they run in
    want.sort(key=lambda i: _ORDER.get((fresh.get(i) or {}).get("type"), 9))
    removed, failed, skipped = [], [], []
    for iid in want:
        it = fresh.get(iid)
        if it is None or not it["cleanable"]:
            skipped.append(iid)
            continue
        try:
            await _remove(it)
            removed.append(iid)
        except Exception as e:  # noqa: BLE001 — report it, carry on with the rest
            failed.append({"id": iid, "error": str(e)[:300]})
    _cache = None
    return {"removed": removed, "failed": failed, "skipped": skipped}


async def _remove(it: dict) -> None:
    t = it["type"]
    if t == "container":
        from . import docker_runtime as dr
        rc, _, err = await dr.cli.run("rm", "--force", it["name"], timeout=60)
        if rc != 0:
            raise RuntimeError(err.strip()[:200] or f"docker rm exited {rc}")
    elif t == "qemu":
        pid = int(it["detail"]["pid"])
        # re-read: the pid must still be a qemu in the same directory
        if not any(q["pid"] == pid and q["cwd"] == it["detail"]["cwd"]
                   for q in _qemu_procs()):
            raise RuntimeError("the process is gone or changed")
        os.kill(pid, signal.SIGKILL)
    elif t in ("box_dir", "sock_dir"):
        p = Path(it["detail"]["path"])
        root = settings.vm_dir / ("boxes" if t == "box_dir" else "sock")
        if p.is_symlink() or _resolved(p) is None or _resolved(p).parent != _resolved(root):
            raise RuntimeError("not a directory of this server's vm_dir")
        shutil.rmtree(p)
    elif t == "overlay":
        from . import lifecycle
        lock = getattr(lifecycle.vm, "_lock", None)
        if lifecycle.vm.running() or (lock is not None and lock.locked()):
            raise RuntimeError("the shared box is booting or running")
        for name in ("overlay.qcow2", "efi_vars_run.fd"):
            (settings.vm_dir / name).unlink(missing_ok=True)
    else:
        raise RuntimeError(f"{t} is never removed automatically")


def summary_line(res: dict) -> str:
    """The startup log line."""
    n = len(res.get("items") or [])
    if not n:
        return "[boxes] no leftovers"
    kinds: dict[str, int] = {}
    for i in res["items"]:
        kinds[i["type"]] = kinds.get(i["type"], 0) + 1
    what = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
    return (f"[boxes] {n} leftover{'s' * (n != 1)} no box owns ({what}); "
            "see /vms (GET /api/vm/leftovers)")


def reset() -> None:
    """Tests only."""
    global _cache
    _cache = None
