"""Memory guard for what the agent runs inside a box.

The 2026-10-01 crash ("guest closed the connection mid-turn", benchmark-game) was
a headless chromium taking a whole 1280 MB desktop VM below its free-memory
watermark: the guest kernel's box-wide OOM killer ran, the VM's vsock transport
stopped answering, and the run-turn server never came back. The turn died with no
`final`, and every later turn on that box timed out until the box was restarted.
What the agent starts has to hit its OWN ceiling long before the box does:

  * KVM guest (root): every process run_code and screenshot start joins one memory
    cgroup, `jav3-work`, capped at the guest's RAM minus a reserve for the kernel,
    the run-turn server and the vsock buffers. When the workload outgrows it the
    cgroup's OOM killer takes one of ITS processes and the guest stays whole.
  * every box: those processes run at the highest oom_score_adj, so if a box-wide
    OOM does happen (a docker box is not root and has a read-only cgroup tree, so
    this is all it gets) the kernel picks the workload, never the turn server.

Pure stdlib: this file runs verbatim in the guest (guest_pkg._COPY_MODULES).
"""
import os

CGROUP_ROOT = "/sys/fs/cgroup"
WORK = "jav3-work"
RESERVE_MB = 192          # what the kernel, the run-turn server and vsock keep, at least
RESERVE_PCT = 20          # ...or this share of the guest's RAM, whichever is more
# Why the reserve is not smaller (measured 2026-10-01, 1280 MB desktop box, guest 1218 MB,
# cap 975): an idle guest uses 148 MB (kernel, run-turn and shell servers), and with the
# cgroup at its cap MemAvailable bottomed out at 69-85 MB. The 243 MB reserve is that 148
# plus about 95 of slack; there is nothing to hand back to the cap.
MIN_LIMIT_MB = 128
OOM_ADJ = b"1000"         # /proc/<pid>/oom_score_adj: raising it never needs a privilege

_work: str | None = None  # the work cgroup's directory, once made
_tried = False
_limit_mb = 0


def meminfo_total_mb(path: str = "/proc/meminfo") -> int:
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def limit_for(total_mb: int) -> int:
    """The cap, in MB, for everything the agent runs on a guest of `total_mb`."""
    return max(MIN_LIMIT_MB, total_mb - max(RESERVE_MB, total_mb * RESERVE_PCT // 100))


def make_cgroup(root: str, total_mb: int) -> str | None:
    """Create `<root>/jav3-work` capped for a guest of `total_mb` MB. Its path,
    or None when the cgroup tree is not there or not writable."""
    d = os.path.join(root, WORK)
    try:
        os.makedirs(d, exist_ok=True)
        if not os.path.exists(os.path.join(d, "memory.max")):
            # the memory controller is not delegated to the root's children yet
            try:
                with open(os.path.join(root, "cgroup.subtree_control"), "w") as f:
                    f.write("+memory")
            except OSError:
                pass
        with open(os.path.join(d, "memory.max"), "w") as f:
            f.write(str(limit_for(total_mb) * 1024 * 1024))
    except OSError:
        return None
    return d


def setup() -> str | None:
    """Make the work cgroup once per process (the parent calls this before it
    spawns). None where there is none: not root (a docker box), no cgroup v2, a
    read-only tree. Never raises."""
    global _work, _tried, _limit_mb
    if _tried:
        return _work
    _tried = True
    try:
        total = meminfo_total_mb()
        if os.geteuid() == 0 and total > 0 and os.path.exists(
                os.path.join(CGROUP_ROOT, "cgroup.controllers")):
            _work = make_cgroup(CGROUP_ROOT, total)
            if _work:
                _limit_mb = limit_for(total)
    except Exception:  # noqa: BLE001 -- a guard that cannot arm must not stop the run
        _work = None
    return _work


def confine() -> None:
    """preexec_fn for a process the agent controls: join the work cgroup and take
    the highest oom_score_adj. Raw os calls (no Python-level locks after fork).
    Never raises."""
    pid = str(os.getpid()).encode()
    for path, data in ((os.path.join(_work, "cgroup.procs") if _work else None, pid),
                       ("/proc/self/oom_score_adj", OOM_ADJ)):
        if path is None:
            continue
        try:
            fd = os.open(path, os.O_WRONLY)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError:
            pass


def confine_session(sid: int, proc_root: str = "/proc") -> int:
    """Put every process of session `sid` at the top of the OOM list. Chromium
    sets its own children's oom_score_adj (renderers 300, measured 2026-10-01 on
    the Pi), which ranks them BEHIND everything else in the work cgroup: at its
    limit the kernel killed the node dev server and the python that was watching
    (adj 1000) while a 700 MB renderer lived on. How many it changed; never raises."""
    n = 0
    try:
        names = os.listdir(proc_root)
    except OSError:
        return 0
    for d in names:
        if not d.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, d, "stat")) as f:
                st = f.read()
            if int(st[st.rindex(")") + 2:].split()[3]) != sid:
                continue
            path = os.path.join(proc_root, d, "oom_score_adj")
            with open(path) as f:
                if f.read().strip() == OOM_ADJ.decode():
                    continue
            with open(path, "w") as f:
                f.write(OOM_ADJ.decode())
            n += 1
        except (OSError, ValueError, IndexError):
            continue
    return n


def _events_path() -> str | None:
    """The memory.events to watch: the work cgroup's, else this container's own
    (a docker box's cgroup root)."""
    p = os.path.join(_work, "memory.events") if _work else os.path.join(
        CGROUP_ROOT, "memory.events")
    return p if os.path.exists(p) else None


def oom_kills(path: str | None = None) -> int | None:
    """How many processes the kernel has OOM-killed in that cgroup so far."""
    path = path or _events_path()
    if not path:
        return None
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.partition(" ")
                if k == "oom_kill":
                    return int(v)
    except (OSError, ValueError):
        pass
    return None


def limit_now_mb() -> int:
    """The cap on what a command may use, for the model's note."""
    if _limit_mb:
        return _limit_mb
    try:
        with open(os.path.join(CGROUP_ROOT, "memory.max")) as f:
            raw = f.read().strip()
        return int(raw) // (1024 * 1024) if raw.isdigit() else 0
    except (OSError, ValueError):
        return 0


def oom_note(kills: int, limit_mb: int | None = None) -> str:
    """What to tell the model when the kernel killed `kills` process(es) for
    memory while its command ran: a bare `Killed` taught it nothing."""
    cap = limit_now_mb() if limit_mb is None else limit_mb
    return (f"[out of memory: the kernel killed {kills} process{'es' if kills != 1 else ''} "
            "while this command ran"
            + (f", for passing the box's {cap} MB limit on what a command may use" if cap
               else ", for running the box out of memory")
            + ". The turn and the box are fine. Use less memory (a smaller page or test, "
              "fewer parallel workers, `node --max-old-space-size=N`), or ask the operator "
              "for a bigger box (the project's Runs in memory).]")
