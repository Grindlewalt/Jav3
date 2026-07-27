"""run_code: execute python/shell INSIDE the disposable guest VM.

This handler only ever executes in the guest (pushed there like the other
in-guest tools). The guest holds no key, no DB, no secrets and has no NIC, so
arbitrary code detonates next to nothing — that inversion is the whole reason
this tool can exist. On the host the same file self-guards and refuses.

Files the run creates or modifies under the workspace copy are captured via the
same `writes` buffer as the file tools, so artifacts ride the turn-end
reconcile -> secret-scan -> advisory-diff-gate path back to the canonical
project files. The rlimits keep the shared guest responsive; they are not the
security boundary (the VM is).
"""
import asyncio
import os
import resource
import signal
import time

from backend import writes
from backend.agent.tools import toolctx
from backend.config import settings

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
OUT_CAP = 6_000                     # chars kept per stream (head + tail)
ARTIFACT_FILE_CAP = 2 * 1024 * 1024   # per-file capture cap
ARTIFACT_TOTAL_CAP = 8 * 1024 * 1024  # total capture cap per run
SKIP_TOP = {".staging", ".git"}     # never captured as artifacts


def _limits() -> None:
    # best-effort: a limit that can't apply must not kill the run — the VM is
    # the boundary, these just keep the one shared guest responsive
    for limit, val in (
        (resource.RLIMIT_CPU, 120),
        (resource.RLIMIT_AS, 512 * 1024 * 1024),
        (resource.RLIMIT_NPROC, 512),
        (resource.RLIMIT_FSIZE, 64 * 1024 * 1024),
    ):
        try:
            resource.setrlimit(limit, (val, val))
        except (ValueError, OSError):
            pass
    os.setsid()                     # own process group, so timeout kills all of it


def _cap(text: str, label: str) -> str:
    if len(text) <= OUT_CAP:
        return text
    half = OUT_CAP // 2
    return (text[:half] + f"\n...[{label} truncated: {len(text):,} chars total — "
            "write long output to a file instead]...\n" + text[-half:])


def _snapshot(root) -> dict:
    snap = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in SKIP_TOP:
            continue
        if p.is_file():
            st = p.stat()
            snap[str(rel)] = (st.st_mtime_ns, st.st_size)
    return snap


async def _capture_artifacts(root, before: dict, slug: str) -> tuple[list[str], list[str]]:
    """Capture files the run created/changed. Returns (captured, skipped)."""
    captured, skipped, total = [], [], 0
    for rel, sig in sorted(_snapshot(root).items()):
        if before.get(rel) == sig:
            continue
        p = root / rel
        size = sig[1]
        if size > ARTIFACT_FILE_CAP or total + size > ARTIFACT_TOTAL_CAP:
            skipped.append(rel)
            continue
        try:
            await writes.apply_write(slug, rel, p.read_bytes())
        except ValueError:          # protected path (or refused) — never kept
            skipped.append(rel)
            continue
        total += size
        captured.append(rel)
    return captured, skipped


async def run(code: str = "", command: str = "", timeout_seconds: int = 0) -> str:
    if not getattr(settings, "in_guest", False):
        return ("error: run_code only executes inside the sandbox guest. The "
                "guest loop is not active on this turn — tell the operator if "
                "you believe it should be.")
    code, command = (code or "").strip(), (command or "").strip()
    if bool(code) == bool(command):
        return "error: pass exactly one of `code` (python) or `command` (shell)."
    timeout = min(int(timeout_seconds) or DEFAULT_TIMEOUT, MAX_TIMEOUT)

    slug = await toolctx.active_slug()
    if slug:
        cwd = settings.projects_dir / slug
        cwd.mkdir(parents=True, exist_ok=True)
        before = _snapshot(cwd)
    else:
        cwd = settings.projects_dir / "_scratch"
        cwd.mkdir(parents=True, exist_ok=True)
        before = None               # no project: nothing to stage artifacts into

    argv = (["python3", "-c", code] if code else ["/bin/sh", "-c", command])
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(cwd),
           "PYTHONUNBUFFERED": "1", "LANG": "C.UTF-8"}
    # Monitored egress: point every subprocess (pip/npm/curl/git) at the host
    # egress proxy so its traffic is policy-checked, secret-injected and watched.
    # Set by the guest boot when JARVIS_VM_EGRESS is on; absent = netless guest,
    # where direct sockets fail closed anyway.
    _proxy = os.environ.get("JARVIS_EGRESS_PROXY")
    if _proxy:
        # Loopback must NEVER route to the proxy: proxy-side "localhost" is the
        # HOST (SSRF guard rightly refuses it), and the agent testing its own
        # in-VM server would see nothing but 403s and conclude "no internet".
        _local = "localhost,127.0.0.1,::1"
        env.update(HTTP_PROXY=_proxy, HTTPS_PROXY=_proxy,
                   http_proxy=_proxy, https_proxy=_proxy,
                   NO_PROXY=_local, no_proxy=_local)
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=env, preexec_fn=_limits,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL)
    except OSError as e:
        return f"error: could not start the process: {e}"

    # Drain the pipes into buffers with our OWN tasks, then wait on process
    # exit with a timeout. On timeout we SIGKILL the group and still await the
    # readers, so output emitted before the kill survives (wait_for around
    # communicate() would discard it when it cancels the read mid-stream).
    async def drain(stream) -> bytes:
        chunks = []
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    out_task = asyncio.ensure_future(drain(proc.stdout))
    err_task = asyncio.ensure_future(drain(proc.stderr))

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        await proc.wait()
    out_b, err_b = await out_task, await err_task
    dur = time.monotonic() - t0

    out = _cap(out_b.decode(errors="replace"), "stdout")
    err = _cap(err_b.decode(errors="replace"), "stderr")
    lines = [f"exit {proc.returncode} · {dur:.2f}s"
             + (f" · KILLED after {timeout}s timeout" if timed_out else "")]
    if out.strip():
        lines += ["--- stdout ---", out.rstrip()]
    if err.strip():
        lines += ["--- stderr ---", err.rstrip()]
    if not out.strip() and not err.strip():
        lines.append("(no output)")

    # network failures here are almost always the monitored-egress gate, not a
    # permanent wall — surface the fix instead of letting the model give up.
    combined = (out + "\n" + err).lower()
    net_markers = ("temporary failure resolving", "could not resolve host",
                   "name or service not known", "network is unreachable",
                   "connection refused", "failed to establish a new connection",
                   "no route to host", "proxyerror", "connection timed out",
                   "could not resolve proxy")
    proxy_on = bool(os.environ.get("JARVIS_EGRESS_PROXY"))
    if proc.returncode != 0 and any(m in combined for m in net_markers):
        if proxy_on:
            lines.append(
                "[network blocked: the VM has monitored egress ON, but the host(s) "
                "this command reached are not on the project's allowlist yet — they "
                "are now QUEUED for the operator to approve in the Network tab. Name "
                "the exact hosts you need and ask the operator to approve them, then "
                "re-run this command.]")
        else:
            lines.append(
                "[network blocked: the VM has no internet right now (monitored egress "
                "is OFF). Name the exact hosts this needs (e.g. github.com, pypi.org) "
                "and tell the operator to enable egress + approve them in the Network "
                "tab. Do NOT just conclude the sandbox has no network and stop.]")

    if before is not None:
        captured, skipped = await _capture_artifacts(cwd, before, slug)
        if captured:
            lines.append(f"kept {len(captured)} changed file(s): "
                         + ", ".join(captured[:10])
                         + (" …" if len(captured) > 10 else ""))
        if skipped:
            lines.append(f"NOT kept (too big / protected): {', '.join(skipped[:5])}")
    else:
        lines.append("(no project active — files written by this run are not kept)")
    return "\n".join(lines)
