"""Guest half of approved persistence: find the hot-plugged /persist disk,
format it the first time, mount it, unmount it, and tell the agent it exists.

The host decides everything (backend/vm/persist.py): whether the disk is
plugged at all, and whether it is read-only at the QEMU block layer. This side
only mounts what it is given — `noexec,nodev,nosuid` always, and `ro,noload`
when the host plugged it read-only (noload: a dirty journal must not be
replayed onto a device that refuses writes). Stdlib + util-linux/e2fsprogs
only, both in every Debian image, so no golden-image rebuild is needed.
"""
import os
import subprocess
import time
from pathlib import Path

SERIAL = "jpersist"                 # matches backend/vm/persist.py
MOUNT = Path("/persist")
_OPTS = "noexec,nodev,nosuid,noatime"


def _find_device(timeout: float = 15.0) -> Path | None:
    """The block device whose virtio serial is ours. sysfs rather than
    /dev/disk/by-id so it doesn't depend on udev having run yet."""
    deadline = time.monotonic() + timeout
    while True:
        for blk in sorted(Path("/sys/block").glob("vd*")):
            try:
                if (blk / "serial").read_text().strip() == SERIAL:
                    dev = Path("/dev") / blk.name
                    if dev.exists():
                        return dev
            except OSError:
                continue
        if time.monotonic() > deadline:
            return None
        time.sleep(0.25)


def _blank(dev: Path) -> bool:
    """All zeros in the first MiB: a never-written qcow2. Anything else is
    somebody's data (or an attack on it) and is never formatted over."""
    with open(dev, "rb") as f:
        head = f.read(1 << 20)
    return not head.strip(b"\0")


def _mounted() -> bool:
    try:
        return any(line.split()[1] == str(MOUNT)
                   for line in Path("/proc/mounts").read_text().splitlines())
    except (OSError, IndexError):
        return False


def mount(read_only: bool, fresh: bool) -> dict:
    """Blocking; run in a thread. Returns {mounted, formatted, error}."""
    if _mounted():
        unmount()                    # a stale mount from a lost release
    dev = _find_device()
    if dev is None:
        return {"type": "persist", "mounted": False,
                "error": "persist disk did not appear in the guest"}
    formatted = False
    try:
        if fresh and not read_only and _blank(dev):
            subprocess.run(["mkfs.ext4", "-q", "-F", "-L", SERIAL, "-m", "0",
                            "-E", "lazy_itable_init=1,lazy_journal_init=1",
                            str(dev)], check=True, capture_output=True)
            formatted = True
        MOUNT.mkdir(parents=True, exist_ok=True)
        opts = _OPTS + (",ro,noload" if read_only else "")
        subprocess.run(["mount", "-t", "ext4", "-o", opts, str(dev), str(MOUNT)],
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as e:
        err = getattr(e, "stderr", b"") or b""
        return {"type": "persist", "mounted": False, "formatted": formatted,
                "error": f"{type(e).__name__}: {err.decode(errors='replace')[:200] or e}"}
    return {"type": "persist", "mounted": True, "formatted": formatted,
            "read_only": read_only}


def unmount() -> dict:
    """Flush and unmount; lazily if something still holds it open (a stray
    process's cwd) — the host unplugs the device either way."""
    if not _mounted():
        return {"type": "persist", "mounted": False}
    os.sync()
    r = subprocess.run(["umount", str(MOUNT)], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["umount", "-l", str(MOUNT)], capture_output=True)
    return {"type": "persist", "mounted": _mounted()}


def note(fact) -> str:
    """The one line the agent is told, appended to its system prompt only when
    the host actually mounted the disk for this turn. No fact -> nothing: the
    agent is never told persistence exists, let alone how to get it."""
    if not isinstance(fact, dict) or not fact.get("path"):
        return ""
    path = fact["path"]
    if fact.get("read_only"):
        return (f"\n\n## Persistent storage\n{path} is this project's persistent "
                "disk, mounted READ-ONLY for this session. Everything else on "
                "this machine is wiped when the session ends.")
    return (f"\n\n## Persistent storage\n{path} is this project's persistent disk "
            "(the operator approved it). Files you put there survive between "
            "sessions; everything else on this machine is wiped when the session "
            "ends. It is for data, not programs: it is mounted noexec, it becomes "
            "read-only for the rest of a session once you read web content, and "
            "nothing in it is loaded into your context automatically.")
