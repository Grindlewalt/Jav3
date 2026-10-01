"""G1: a command that outgrows its box must not take the box down with it.

2026-10-01, benchmark-game: a headless chromium (a `desktop` KVM box, 1280 MB) ran the
whole VM out of memory. The guest kernel's box-wide OOM killer ran, the VM's vsock
stopped answering, and the turn ended with "guest closed the connection mid-turn";
every later turn on that box then waited out the boot timeout. Reproduced on the Pi
with `chromium --headless` on a page that touches 1500 MB (console: "chromium invoked
oom-killer ... Out of memory: Killed process 821 (chromium)"), and the same page run
inside a memory cgroup under the box's RAM left the turn alive.

memguard puts what run_code / screenshot start into such a cgroup and at the top of
the OOM killer's list; deathnote / death_note say why a guest went; a warm guest that
stops answering is restarted. The cgroup itself is the guest kernel's, so what is
tested here is the contract around it: who joins it, what it is capped at, what the
model is told, and the notes."""
import asyncio
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from backend import memguard
from backend.agent.tools import registry, toolctx
from backend.config import settings
from backend.db import init_db
from backend.vm import boxes, deathnote, guest_pkg, lifecycle

ROOT = Path(__file__).resolve().parent.parent
HAVE_PROC_ADJ = os.path.exists("/proc/self/oom_score_adj")

# lines from the 2026-10-01 reproduction's console (KVM guest, kernel 6.12)
GLOBAL_OOM = (
    "[   54.562851] chromium invoked oom-killer: gfp_mask=0x440dc0(GFP_KERNEL_ACCOUNT|"
    "__GFP_COMP|__GFP_ZERO), order=0, oom_score_adj=300\n"
    "[   54.842134] oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,"
    "mems_allowed=0,global_oom,task_memcg=/system.slice/jarvis-guest.service,"
    "task=chromium,pid=821,uid=0\n"
    "[   54.847980] Out of memory: Killed process 821 (chromium) total-vm:1518016596kB, "
    "anon-rss:961388kB, file-rss:28kB, shmem-rss:724kB, UID:0 pgtables:3116kB "
    "oom_score_adj:300\n")
MEMCG_OOM = (
    "[   80.978507] oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/,"
    "mems_allowed=0,oom_memcg=/jav3-work,task_memcg=/jav3-work,task=chromium,pid=669,uid=0\n"
    "[   80.982360] Memory cgroup out of memory: Killed process 669 (chromium) "
    "total-vm:1518016596kB, anon-rss:736612kB, file-rss:1740kB, shmem-rss:836kB, UID:0 "
    "pgtables:2564kB oom_score_adj:300\n")


# --- the cap -----------------------------------------------------------------------

def test_the_cap_leaves_the_box_a_reserve():
    assert memguard.limit_for(1218) == 975          # the 1280 MB desktop box: 20% kept
    assert memguard.limit_for(700) == 508           # a 768 MB box: the 192 MB floor
    assert memguard.limit_for(4096) == 3277
    assert memguard.limit_for(100) == memguard.MIN_LIMIT_MB   # never nothing


def test_make_cgroup_creates_the_capped_group(tmp_path):
    d = memguard.make_cgroup(str(tmp_path), 1218)
    assert d == str(tmp_path / "jav3-work")
    assert (tmp_path / "jav3-work" / "memory.max").read_text() == str(975 * 1024 * 1024)
    # the memory controller was asked for when the new group had no memory.max
    assert (tmp_path / "cgroup.subtree_control").read_text() == "+memory"


def test_make_cgroup_gives_up_quietly_where_the_tree_is_not_writable(tmp_path):
    (tmp_path / "blocked").write_text("a file where the root should be")
    assert memguard.make_cgroup(str(tmp_path / "blocked"), 1218) is None


@pytest.fixture
def fresh_state(monkeypatch):
    monkeypatch.setattr(memguard, "_work", None)
    monkeypatch.setattr(memguard, "_tried", False)
    monkeypatch.setattr(memguard, "_limit_mb", 0)


def test_setup_arms_for_root_on_cgroup_v2_and_is_cached(tmp_path, monkeypatch, fresh_state):
    (tmp_path / "cgroup.controllers").write_text("cpuset cpu io memory pids")
    monkeypatch.setattr(memguard, "CGROUP_ROOT", str(tmp_path))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(memguard, "meminfo_total_mb", lambda path="": 1218)
    assert memguard.setup() == str(tmp_path / "jav3-work")
    assert memguard.limit_now_mb() == 975
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert memguard.setup() == str(tmp_path / "jav3-work")       # decided once


def test_setup_does_nothing_for_a_docker_box(tmp_path, monkeypatch, fresh_state):
    """uid 10001, a read-only cgroup tree: no cgroup (the box has the container's own)."""
    (tmp_path / "cgroup.controllers").write_text("memory")
    monkeypatch.setattr(memguard, "CGROUP_ROOT", str(tmp_path))
    monkeypatch.setattr(os, "geteuid", lambda: 10001)
    monkeypatch.setattr(memguard, "meminfo_total_mb", lambda path="": 1218)
    assert memguard.setup() is None
    assert not (tmp_path / "jav3-work").exists()


# --- who joins it ------------------------------------------------------------------

def test_confine_joins_the_group_and_takes_the_top_oom_priority(tmp_path):
    """Run in a child: confine() changes the process it runs in."""
    d = tmp_path / "jav3-work"
    d.mkdir()
    (d / "cgroup.procs").write_text("")
    code = ("import os, sys; sys.path.insert(0, sys.argv[1]);"
            "from backend import memguard; memguard._work = sys.argv[2]; memguard.confine();"
            "p = '/proc/self/oom_score_adj';"
            "print(os.getpid(), open(p).read().strip() if os.path.exists(p) else 'n/a')")
    out = subprocess.run([sys.executable, "-c", code, str(ROOT), str(d)],
                         capture_output=True, text=True, timeout=30, check=True).stdout.split()
    assert (d / "cgroup.procs").read_text() == out[0]            # its pid, written to the group
    if HAVE_PROC_ADJ:
        assert out[1] == "1000"                                  # first pick for the OOM killer


def test_confine_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(memguard, "_work", str(tmp_path / "missing"))
    monkeypatch.setattr(os, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    memguard.confine()                                           # nothing to assert: no raise


# --- the model is told ---------------------------------------------------------------

def test_oom_kills_reads_the_cgroups_counter(tmp_path):
    p = tmp_path / "memory.events"
    p.write_text("low 0\nhigh 0\nmax 3564\noom 1\noom_kill 1\noom_group_kill 0\n")
    assert memguard.oom_kills(str(p)) == 1
    assert memguard.oom_kills(str(tmp_path / "nope")) is None
    p.write_text("max 0\n")
    assert memguard.oom_kills(str(p)) is None


def test_the_note_names_the_limit_and_the_way_out():
    note = memguard.oom_note(1, 975)
    assert "killed 1 process while this command ran" in note and "975 MB" in note
    assert "turn and the box are fine" in note and "bigger box" in note
    assert "2 processes" in memguard.oom_note(2, 0) and "975" not in memguard.oom_note(2, 0)


def _guest(monkeypatch, tmp_path, slug="proj"):
    monkeypatch.setattr(settings, "in_guest", True)
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    (tmp_path / slug).mkdir(parents=True, exist_ok=True)

    async def fake_slug():
        return slug
    monkeypatch.setattr(toolctx, "active_slug", fake_slug)


@pytest.fixture
def cg(tmp_path, monkeypatch):
    """Stand-in for the work cgroup: plain files with the cgroup's names."""
    d = tmp_path / "wg" / "jav3-work"
    d.mkdir(parents=True)
    (d / "cgroup.procs").write_text("")
    (d / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
    monkeypatch.setattr(memguard, "_work", str(d))
    monkeypatch.setattr(memguard, "_tried", True)
    monkeypatch.setattr(memguard, "_limit_mb", 975)
    return d


async def test_run_code_puts_its_process_in_the_work_group(tmp_env, monkeypatch, tmp_path, cg):
    await init_db()
    _guest(monkeypatch, tmp_path)
    out = await registry.dispatch("run_code", {"command": "echo pid=$$"})
    pid = out.split("pid=")[1].split()[0]
    assert (cg / "cgroup.procs").read_text() == pid


@pytest.mark.skipif(not HAVE_PROC_ADJ, reason="needs /proc/<pid>/oom_score_adj (Linux)")
async def test_run_code_commands_are_the_first_pick_of_the_oom_killer(
        tmp_env, monkeypatch, tmp_path, cg):
    """Before: the command inherited the turn server's 0 (docker: 500), so a big
    enough pile of small processes could make the run-turn server the victim."""
    await init_db()
    _guest(monkeypatch, tmp_path)
    out = await registry.dispatch("run_code", {"command": "cat /proc/self/oom_score_adj"})
    assert out.split("--- stdout ---")[1].split()[0] == "1000"


async def test_run_code_says_when_the_kernel_killed_something(tmp_env, monkeypatch, tmp_path, cg):
    await init_db()
    _guest(monkeypatch, tmp_path)
    out = await registry.dispatch("run_code", {
        "command": f"printf 'low 0\\nmax 9\\noom 2\\noom_kill 2\\n' > {cg}/memory.events; "
                   "echo done"})
    assert "exit 0" in out
    assert "[out of memory: the kernel killed 2 processes while this command ran" in out
    assert "975 MB" in out


async def test_run_code_adds_nothing_when_nothing_was_killed(tmp_env, monkeypatch, tmp_path, cg):
    await init_db()
    _guest(monkeypatch, tmp_path)
    out = await registry.dispatch("run_code", {"command": "echo fine"})
    assert "out of memory" not in out
    # a box with no counter to read (not a guest with cgroups): no note and no crash
    monkeypatch.setattr(memguard, "_work", None)
    monkeypatch.setattr(memguard, "CGROUP_ROOT", str(tmp_path / "none"))
    out = await registry.dispatch("run_code", {"command": "echo fine"})
    assert "fine" in out and "out of memory" not in out


def test_the_guest_package_ships_memguard():
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(guest_pkg.build_package_tar()), mode="r:gz") as t:
        names = set(t.getnames())
    assert "backend/memguard.py" in names and "tools/run_code/handler.py" in names


# --- the notes ------------------------------------------------------------------------

def test_console_note_names_the_process_the_kernel_killed():
    note = deathnote.console_note("boot noise\n" + GLOBAL_OOM)
    assert "chromium (pid 821, 938 MB)" in note and "ran out of memory" in note
    assert "55 s after boot" in note


def test_console_note_tells_a_limit_kill_from_a_box_wide_one():
    note = deathnote.console_note(GLOBAL_OOM + MEMCG_OOM)
    assert "pid 669, 719 MB" in note and "passing its memory limit" in note
    assert "kill 2 since boot" in note


def test_console_note_reports_a_panic_and_stays_quiet_otherwise():
    assert "Kernel panic - not syncing: out of memory" in deathnote.console_note(
        "[  71.1] Kernel panic - not syncing: out of memory\n")
    assert deathnote.console_note("login: \n[ 3.2] eth0: link up\n") == ""
    assert deathnote.console_note("") == ""


def test_docker_note_and_message_shapes():
    st = {"Status": "exited", "Running": False, "OOMKilled": True, "ExitCode": 137}
    n = deathnote.docker_note(st, 512, "Killed")
    assert n == ("the container exited (exit code 137, killed); a process in it was killed "
                 "for running out of memory (limit 512 MB); last output: Killed")
    assert deathnote.docker_note(None, None, "") == "the container is gone"
    assert deathnote.with_note("guest closed", "") == "guest closed"
    assert deathnote.with_note("guest closed", "the x") == "guest closed. The x"


# --- KVM: the controller reads its console, and restarts a wedged guest ---------------

@pytest.fixture
def kvm(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_max_boxes", 8)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 3)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 10**6)
    boxes.registry.reset()
    settings.vm_dir.mkdir(parents=True, exist_ok=True)
    yield
    boxes.registry.reset()


async def test_death_note_reads_the_console_tail(kvm):
    vm = lifecycle.GuestVM()
    assert await vm.death_note() == ""                           # no console yet
    (settings.vm_dir / "console.log").write_text("x" * 600_000 + GLOBAL_OOM)
    assert "chromium (pid 821" in await vm.death_note()          # found past a long head
    vm._proc = types.SimpleNamespace(returncode=137)
    assert "QEMU exit code 137" in await vm.death_note()


def _readiness(monkeypatch, ctl, *, answers_after):
    """Fake the vsock probe: it refuses until `answers_after()` says the guest is back."""
    class Sock:
        def __init__(self, *a):
            pass

        def connect(self, addr):
            if not answers_after():
                raise ConnectionResetError(104, "no answer")

        def close(self):
            pass
    real_sleep = asyncio.sleep

    async def quick(_s):
        await real_sleep(0)
    monkeypatch.setattr(lifecycle, "base_built", lambda: True)
    monkeypatch.setattr(lifecycle.gateway, "enabled", True)
    monkeypatch.setattr(lifecycle.socket, "socket", Sock)
    monkeypatch.setattr(lifecycle.socket, "AF_VSOCK", 40, raising=False)
    monkeypatch.setattr(lifecycle.asyncio, "sleep", quick)
    calls = []

    async def boot():
        calls.append("boot")

    async def teardown():
        calls.append("teardown")
    monkeypatch.setattr(ctl, "boot", boot)
    monkeypatch.setattr(ctl, "teardown", teardown)
    return calls


async def test_a_warm_guest_that_stops_answering_is_restarted_once(kvm, monkeypatch):
    """Before: every turn waited out the 120 s boot timeout on a guest the host
    still called running, until someone restarted the box."""
    await init_db()
    box = boxes.allocate("project", project="alpha")
    ctl = boxes.controller(box)
    monkeypatch.setattr(lifecycle, "WEDGED_AFTER_S", 0)
    monkeypatch.setattr(ctl, "running", lambda: True)
    state = {"restarted": False}
    calls = _readiness(monkeypatch, ctl, answers_after=lambda: state["restarted"])
    orig_boot = ctl.boot

    async def boot():
        await orig_boot()
        state["restarted"] = "teardown" in calls                 # answers once it was rebooted
    monkeypatch.setattr(ctl, "boot", boot)
    (box.dir).mkdir(parents=True, exist_ok=True)
    (box.dir / "console.log").write_text(GLOBAL_OOM)
    await ctl._ensure_ready_locked()
    assert calls == ["boot", "teardown", "boot"]
    from backend.vm import boxlog
    ev = (await boxlog.events(box.id))[0]
    assert ev["event"] == "restarted" and ev["actor"] == "app"
    assert "stopped answering" in ev["reason"] and "chromium (pid 821" in ev["reason"]


async def test_a_cold_boot_and_a_busy_guest_are_never_restarted_for_being_slow(kvm, monkeypatch):
    await init_db()
    box = boxes.allocate("project", project="alpha")
    ctl = boxes.controller(box)
    monkeypatch.setattr(lifecycle, "WEDGED_AFTER_S", 0)
    ticks = {"n": 0}

    def answers():
        ticks["n"] += 1
        return ticks["n"] > 3
    monkeypatch.setattr(ctl, "running", lambda: False)           # booting from stopped
    calls = _readiness(monkeypatch, ctl, answers_after=answers)
    await ctl._ensure_ready_locked()
    assert calls == ["boot"]
    ticks["n"] = 0
    monkeypatch.setattr(ctl, "running", lambda: True)            # up, but a turn is pinned
    ctl._inflight = 1
    await ctl._ensure_ready_locked()
    assert calls == ["boot", "boot"]                             # no teardown either time


async def test_the_ready_timeout_carries_the_consoles_last_words(kvm, monkeypatch):
    box = boxes.allocate("project", project="alpha")
    ctl = boxes.controller(box)
    monkeypatch.setattr(settings, "vm_boot_timeout_seconds", 0)
    monkeypatch.setattr(ctl, "running", lambda: False)
    _readiness(monkeypatch, ctl, answers_after=lambda: False)
    box.dir.mkdir(parents=True, exist_ok=True)
    (box.dir / "console.log").write_text(GLOBAL_OOM)
    with pytest.raises(lifecycle.VMError) as e:
        await ctl._ensure_ready_locked()
    assert "did not become ready in time (the guest's console shows" in str(e.value)


# --- C1: chromium lowers its own children's adj ----------------------------------------

def test_confine_session_raises_chromiums_children_to_the_top(tmp_path):
    """2026-10-01 on the Pi: chromium set its renderers to oom_score_adj 300, so at the
    work cgroup's limit the kernel killed the node dev server and the watching python
    (1000) and left a 700 MB renderer alone."""
    def proc(pid, session, adj):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} (chromium (renderer)) S 1 {session} {session} 0 -1 0")
        (d / "oom_score_adj").write_text(f"{adj}\n")
    proc(100, 100, 300)       # the renderer, in the screenshot's session
    proc(101, 100, 1000)      # already there
    proc(102, 999, 0)         # someone else's: untouched
    (tmp_path / "self").mkdir()
    assert memguard.confine_session(100, str(tmp_path)) == 1
    assert (tmp_path / "100" / "oom_score_adj").read_text() == "1000"
    assert (tmp_path / "102" / "oom_score_adj").read_text() == "0\n"
    assert memguard.confine_session(100, str(tmp_path)) == 0          # idempotent
    assert memguard.confine_session(100, str(tmp_path / "nope")) == 0  # never raises
