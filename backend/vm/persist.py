"""Approved persistence inside the guest VM.

The guest is disposable: its overlay is discarded on scrub/nuke and every turn
gets a fresh copy of the project pushed in. That stays true. What this adds is
ONE opt-in exception, per project: a small data disk that survives.

    <vm_dir>/persist/<slug>.qcow2   sparse qcow2, virtual size = vm_persist_max_mb

It is hot-plugged into the running guest (QMP `blockdev-add` + `device_add` onto
the spare `jpersist_rp` PCIe root port run_vm.sh reserves) and mounted at
/persist `noexec,nodev,nosuid` only while a turn of an APPROVED project is live:

  * approval is `projects.persist_approved`, set only by the operator's cookie
    session (projects.py) — the agent has no tool for it, and it is not in
    `.workspace.json`, which the agent can write;
  * an incognito turn never attaches it, nor does a nested turn (it runs in
    its parent's guest and sees whatever the parent attached);
  * ONE project holds /persist at a time. Concurrent top-level turns of the
    same project share the mount (refcounted, like the workspace holds in
    guest_turn); a turn of a different project while it is held simply runs
    without persistence;
  * the last turn out unmounts it in the guest and unplugs it host-side, so the
    idle scrub (which only runs with no turn in flight) never kills a guest
    with the disk attached;
  * taint: every turn starts clean and only becomes tainted mid-turn, when it
    reads the web or a peer's message. At that moment (broker_dispatch, BEFORE
    the tainted result reaches the guest) the disk is unmounted, unplugged and
    re-plugged READ-ONLY at the QEMU block layer. So content the agent writes
    after reading attacker-authorable text cannot land on the disk, and a
    compromised guest cannot remount its way back: the host node itself is
    read-only. It returns read-write only on the next fresh attach;
  * a guest that will not hand the device back (device_del never completes)
    is torn down — the disk is closed by QEMU exiting. That is the fail-closed
    path, and it raises a security event.

What this does NOT do (see SECURITY-RESIDUAL-RISK.md): while mounted, /persist is
visible to every process in the guest, including a concurrent incognito turn or
another project's turn running code in the same guest; and `noexec` stops
`./implant`, not `python3 /persist/implant.py`.
"""
import asyncio
import json
import os
import re
from pathlib import Path

from ..config import settings

MOUNT = "/persist"                 # where the guest mounts it
SERIAL = "jpersist"                # virtio-blk serial the guest finds it by
ROOT_PORT = "jpersist_rp"          # the spare hotplug port in run_vm.sh
_NODE, _FILE_NODE, _DEV = "jpersist", "jpersist-file", "jpersist-dev"

# projects.slugify only ever produces [a-z0-9-]; anything else is refused
# rather than joined into a path.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")

_MIN_MB = 64                       # ext4 + journal need room to be a filesystem
_QMP_TIMEOUT = 15.0
_MOUNT_TIMEOUT = 60.0              # first mount runs mkfs
_UNMOUNT_TIMEOUT = 20.0
_UNPLUG_TIMEOUT = 15.0


class PersistError(Exception):
    pass


# --- the disk -----------------------------------------------------------------

def disk_dir() -> Path:
    return settings.vm_dir / "persist"


def disk_path(slug: str) -> Path:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise PersistError(f"refusing persist disk for slug {slug!r}")
    return disk_dir() / f"{slug}.qcow2"


def _ready_marker(slug: str) -> Path:
    # written after the first successful mount: until it exists the guest may
    # format a BLANK disk; after, it never formats (a mount failure is reported,
    # not "fixed" by mkfs over the operator's data)
    return disk_dir() / f"{slug}.ready"


def cap_bytes() -> int:
    return max(_MIN_MB, int(settings.vm_persist_max_mb)) * 1024 * 1024


def disk_info(slug: str) -> dict:
    p = disk_path(slug)
    if not p.exists():
        return {"exists": False, "bytes_used": 0, "cap_bytes": cap_bytes()}
    st = p.stat()
    return {"exists": True, "bytes_used": st.st_blocks * 512,
            "cap_bytes": cap_bytes()}


async def _qemu_img(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "qemu-img", *args, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise PersistError(f"qemu-img {args[0]} failed: "
                           f"{err.decode(errors='replace')[:300]}")
    return out.decode(errors="replace")


async def ensure_disk(slug: str) -> bool:
    """Create the project's disk if it doesn't exist. Returns True when the disk
    has never been mounted (the guest may format it). An existing disk bigger
    than the cap is refused, not attached: the cap is only a cap if a swapped or
    stale file can't exceed it."""
    p = disk_path(slug)
    if p.exists():
        info = json.loads(await _qemu_img("info", "--output=json", str(p)))
        if info.get("format") != "qcow2":
            raise PersistError(f"{p.name} is not a qcow2 image")
        if info.get("backing-filename"):
            raise PersistError(f"{p.name} has a backing file; refusing it")
        if int(info.get("virtual-size") or 0) > cap_bytes():
            raise PersistError(
                f"{p.name} is larger than vm_persist_max_mb; delete it (revoke "
                "and delete) or raise the cap")
        return not _ready_marker(slug).exists()
    disk_dir().mkdir(parents=True, exist_ok=True)
    os.chmod(disk_dir(), 0o700)
    await _qemu_img("create", "-f", "qcow2", str(p), str(cap_bytes()))
    os.chmod(p, 0o600)
    _ready_marker(slug).unlink(missing_ok=True)
    return True


def delete_disk(slug: str) -> bool:
    """Remove the disk (and its marker). Refuses while it is attached."""
    if _state.holder == slug or _state.lock.locked():
        raise PersistError("the disk is attached to (or being attached for) a "
                           "live turn")
    p = disk_path(slug)
    existed = p.exists()
    p.unlink(missing_ok=True)
    _ready_marker(slug).unlink(missing_ok=True)
    return existed


# --- approval (the operator's, via projects.py) -----------------------------------

async def approved(slug: str | None) -> bool:
    if not slug or not settings.vm_persist_enabled:
        return False
    from ..db import get_db
    db = await get_db()
    try:
        async with db.execute(
                "SELECT persist_approved FROM projects WHERE slug = ? "
                "AND deleted_at IS NULL", (slug,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    return bool(row and row["persist_approved"])


# --- QMP ------------------------------------------------------------------------

def qmp_path() -> Path:
    return settings.vm_dir / "qmp.sock"


async def qmp(commands: list[dict]) -> list[dict]:
    """Run QMP commands against the guest's monitor socket, in order. Returns
    one reply per command ({"return": ...} or {"error": ...}); async events
    interleaved on the socket are skipped."""
    async def _run() -> list[dict]:
        reader, writer = await asyncio.open_unix_connection(str(qmp_path()))
        try:
            async def reply() -> dict:
                while True:
                    line = await reader.readline()
                    if not line:
                        raise PersistError("QMP connection closed")
                    msg = json.loads(line)
                    if "return" in msg or "error" in msg:
                        return msg

            greeting = json.loads(await reader.readline() or b"{}")
            if "QMP" not in greeting:
                raise PersistError("no QMP greeting")
            out = []
            for cmd in [{"execute": "qmp_capabilities"}, *commands]:
                writer.write((json.dumps(cmd) + "\n").encode())
                await writer.drain()
                out.append(await reply())
            return out[1:]
        finally:
            writer.close()
    try:
        return await asyncio.wait_for(_run(), _QMP_TIMEOUT)
    except (OSError, ValueError, asyncio.TimeoutError) as e:
        raise PersistError(f"QMP: {type(e).__name__}: {e}") from e


def _err(r: dict) -> str | None:
    e = r.get("error")
    return f"{e.get('class')}: {e.get('desc')}" if e else None


async def _plug(slug: str, read_only: bool) -> None:
    """blockdev-add the disk (read-only at the QEMU block layer when asked —
    the guest cannot undo that) and hot-plug it on the reserved root port."""
    path = str(disk_path(slug))
    add, dev = await qmp([
        {"execute": "blockdev-add", "arguments": {
            "driver": "qcow2", "node-name": _NODE, "read-only": read_only,
            "file": {"driver": "file", "node-name": _FILE_NODE,
                     "filename": path, "read-only": read_only}}},
        {"execute": "device_add", "arguments": {
            "driver": "virtio-blk-pci", "id": _DEV, "drive": _NODE,
            "bus": ROOT_PORT, "serial": SERIAL}},
    ])
    if _err(add):
        raise PersistError(f"blockdev-add: {_err(add)}")
    if _err(dev):
        await qmp([{"execute": "blockdev-del", "arguments": {"node-name": _NODE}}])
        raise PersistError(f"device_add: {_err(dev)}")


async def _unplug() -> None:
    """Ask the guest to release the device, then drop the block node. device_del
    completes only when the guest acks the unplug; until then blockdev-del fails
    with the node in use — so poll it. Raises if the guest never lets go."""
    await qmp([{"execute": "device_del", "arguments": {"id": _DEV}}])
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _UNPLUG_TIMEOUT
    while True:
        (r,) = await qmp([{"execute": "blockdev-del",
                           "arguments": {"node-name": _NODE}}])
        if not _err(r):
            return
        if loop.time() > deadline:
            raise PersistError(f"guest did not release the disk: {_err(r)}")
        await asyncio.sleep(0.5)


# --- guest side (over the run-turn server) -----------------------------------------

async def _guest(spec: dict, timeout: float) -> dict:
    from .guest_turn import _guest_rpc
    try:
        r = await asyncio.wait_for(_guest_rpc(spec), timeout)
    except (OSError, ConnectionError, ValueError, asyncio.TimeoutError) as e:
        return {"mounted": False, "error": f"{type(e).__name__}: {e}"}
    return r or {"mounted": False, "error": "no reply"}


# --- the hold -------------------------------------------------------------------

class _State:
    def __init__(self):
        self.lock = asyncio.Lock()
        # bumped on every reset, so a turn whose hold predates a teardown (nuke,
        # fail-closed) can't release a LATER hold on the same slug
        self.gen = 0
        self.reset()

    def reset(self):
        self.gen += 1
        self.holder: str | None = None     # slug whose disk is attached
        self.count = 0                     # live top-level turns sharing it
        self.read_only = False
        self.plugged = False               # device currently on the guest bus


_state = _State()


def holder() -> str | None:
    return _state.holder


def generation() -> int:
    """Identity of the current hold; read right after attach_for_turn and hand
    it back to release_for_turn."""
    return _state.gen


def status() -> dict:
    return {"enabled": settings.vm_persist_enabled, "holder": _state.holder,
            "read_only": _state.read_only, "turns": _state.count}


def forget() -> None:
    """The guest is gone (teardown): QEMU closed the disk, nothing is attached.
    Called from lifecycle.teardown, which may run without our lock (nuke, fail-
    closed) — the state is plain fields, safe on the single event loop."""
    _state.reset()


def _fact() -> dict:
    return {"path": MOUNT, "read_only": _state.read_only}


async def _attach_locked(slug: str, read_only: bool) -> bool:
    fresh = await ensure_disk(slug)
    await _plug(slug, read_only)
    _state.plugged = True
    r = await _guest({"mode": "persist_mount", "persist_ro": read_only,
                      "persist_fresh": fresh and not read_only}, _MOUNT_TIMEOUT)
    if not r.get("mounted"):
        print(f"[persist] mount failed for {slug}: {r.get('error')}")
        await _release_device_locked(slug)
        return False
    if not read_only:
        _ready_marker(slug).touch()
    return True


async def _release_device_locked(slug: str) -> None:
    """Unmount in the guest (best effort — a compromised guest may lie) and
    unplug host-side (authoritative). If the unplug never completes, kill the
    guest: that is what actually closes the disk."""
    await _guest({"mode": "persist_unmount"}, _UNMOUNT_TIMEOUT)
    if not _state.plugged:
        return
    try:
        await _unplug()
        _state.plugged = False
    except PersistError as e:
        await _fail_closed(slug, str(e))


async def _fail_closed(slug: str, reason: str) -> None:
    from .lifecycle import vm
    print(f"[persist] {reason} — tearing the guest down to close the disk")
    await vm.teardown()             # calls forget()
    try:
        from .. import security
        from ..db import get_db
        db = await get_db()
        try:
            await security.raise_event(
                db, kind="persist_unplug_failed", severity="warn", project=slug,
                summary=f"guest did not release /persist for '{slug}'; guest "
                        "VM torn down to close the disk",
                detail={"reason": reason[:300]})
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — the alert must not mask the teardown
        pass


async def attach_for_turn(slug: str) -> dict | None:
    """Take a hold on /persist for one top-level turn of an APPROVED project
    (the caller checked approval). Returns the fact to put in the turn spec, or
    None when the turn runs without persistence (another project holds it, the
    kill switch is off, or attach failed). Never raises: persistence failing
    must not fail the turn."""
    if not settings.vm_persist_enabled:
        return None
    async with _state.lock:
        if _state.holder == slug:
            _state.count += 1
            return _fact()
        if _state.holder is not None:
            return None
        try:
            ok = await _attach_locked(slug, read_only=False)
        except PersistError as e:
            print(f"[persist] attach failed for {slug}: {e}")
            if _state.plugged:
                await _release_device_locked(slug)
            _state.reset()
            return None
        if not ok:
            _state.reset()
            return None
        _state.holder, _state.count, _state.read_only = slug, 1, False
        return _fact()


async def release_for_turn(slug: str, gen: int) -> None:
    """Drop one turn's hold; the last one out unmounts and unplugs."""
    async with _state.lock:
        if _state.holder != slug or _state.gen != gen:
            return                     # the hold this turn took is long gone
        _state.count -= 1
        if _state.count > 0:
            return
        try:
            await _release_device_locked(slug)
        except PersistError as e:
            print(f"[persist] release: {e}")
        _state.reset()


async def on_taint(slug: str | None) -> None:
    """A turn of `slug` just consumed untrusted content: make its /persist
    read-only for the rest of this hold, enforced host-side by re-plugging the
    disk on a read-only block node. Called from broker_dispatch before the
    tainted result is returned to the guest. Never raises."""
    if not slug or _state.holder != slug:
        return                         # the common case: nothing attached
    async with _state.lock:
        if _state.holder != slug or _state.read_only:
            return
        _state.read_only = True     # first, so a failure below still fails closed
        try:
            await _release_device_locked(slug)
            if _state.holder is None:          # fail-closed teardown happened
                return
            # False = the guest failed the ro mount and the device is already
            # unplugged again: the hold stays, with no disk behind it
            await _attach_locked(slug, read_only=True)
        except PersistError as e:
            print(f"[persist] read-only re-attach failed for {slug}: {e}")
