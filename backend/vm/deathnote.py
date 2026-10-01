"""One short sentence on why a box's guest stopped answering mid-turn.

A turn whose stream ended with no `final` used to say only "the guest crashed, ran
out of memory or its VM was reaped" (2026-10-01, benchmark-game): the evidence was
never read. A KVM guest's console keeps the kernel's OOM line; a container keeps its
exit state and last output until it is removed. The controllers (lifecycle.GuestVM,
docker_runtime.DockerBox) read what they can and these format it. Pure: no I/O.
"""
import re

MAX_NOTE = 400

_OOM = re.compile(r"\[\s*([\d.]+)\]\s+(Memory cgroup out of memory|Out of memory): "
                  r"Killed process (\d+) \(([^)]*)\)([^\n]*)")
_RSS = re.compile(r"anon-rss:(\d+)kB")
_PANIC = re.compile(r"\[\s*[\d.]+\]\s+(Kernel panic[^\n]*|Oops[^\n]*|BUG:[^\n]*)")


def console_note(text: str) -> str:
    """From the tail of a KVM guest's console log: the kernel's last OOM kill or a
    panic, or '' when the console says nothing about it."""
    kills = list(_OOM.finditer(text or ""))
    if kills:
        t, kind, pid, name, rest = kills[-1].groups()
        rss = _RSS.search(rest)
        size = f", {int(rss.group(1)) // 1024} MB" if rss else ""
        # the console shows the kill, not that it ended this turn: say what it shows
        why = ("for passing its memory limit" if kind.startswith("Memory cgroup")
               else "because the box ran out of memory")
        what = (f"the guest's console shows its kernel killing {name} (pid {pid}{size}) "
                f"{why}, {float(t):.0f} s after boot")
        if len(kills) > 1:
            what += f" (kill {len(kills)} since boot)"
        return what[:MAX_NOTE]
    panic = list(_PANIC.finditer(text or ""))
    if panic:
        return f"the guest kernel reported: {panic[-1].group(1).strip()[:200]}"
    return ""


def docker_note(state: dict | None, mem_mb: int | None, tail: str) -> str:
    """From `docker inspect {{json .State}}` and the container's last output lines."""
    parts = []
    if state is None:
        parts.append("the container is gone")
    else:
        status = str(state.get("Status") or "")
        code = state.get("ExitCode")
        if state.get("Running"):
            parts.append("the container is still running, so its run-turn server "
                         "dropped the connection")
        else:
            why = f"the container {status or 'is not running'}"
            if isinstance(code, int):
                why += f" (exit code {code}" + (", killed" if code == 137 else "") + ")"
            parts.append(why)
        if state.get("OOMKilled"):
            lim = f" (limit {mem_mb} MB)" if mem_mb else ""
            parts.append(f"a process in it was killed for running out of memory{lim}")
        if state.get("Error"):
            parts.append(f"docker says: {str(state['Error'])[:120]}")
    if tail:
        parts.append(f"last output: {tail[-200:]}")
    return "; ".join(parts)[:MAX_NOTE]


def with_note(message: str, note: str) -> str:
    """`message` plus the note, when there is one."""
    return f"{message}. {note[0].upper() + note[1:]}" if note else message
