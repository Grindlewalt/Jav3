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
import functools
import ipaddress
import os
import re
import resource
import signal
import tempfile
import time

from backend import writes
from backend.agent.tools import toolctx
from backend.config import settings

DEFAULT_TIMEOUT = 60
PIP_VENV_BIN = "/opt/jav3/py/bin"     # images.PIP_VENV: approved pip packages
MAX_TIMEOUT = 300
OUT_CAP = 6_000                     # chars kept per stream (head + tail)
ARTIFACT_FILE_CAP = 2 * 1024 * 1024   # per-file capture cap
ARTIFACT_TOTAL_CAP = 8 * 1024 * 1024  # total capture cap per run
# Never captured as artifacts: our own overlay/vcs, plus dependency and
# package-manager cache trees. Once npm works in-guest a single `npm install`
# would otherwise try to reconcile thousands of node_modules files back into
# the project (each through the secret-scan + diff-gate) — skip them wholesale.
#
# The same trees the turn-end pack drops (workspace_xfer.SKIP_OUT), so nothing is
# reported as "kept" that the host then throws away (BUILD-04: .pytest_cache and
# __pycache__ used to be listed as kept, and agents spent calls cleaning them).
SKIP_DIRS = {".staging", ".git", "node_modules", ".npm", ".cache", ".venv",
             "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
PIPE_GRACE = 2.0     # seconds the pipes get to close after the shell exits


def _group_pids(pgid: int) -> list[int]:
    """Pids still in the run's process group (Linux /proc; [] elsewhere)."""
    pids = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return pids
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                # pid (comm) state ppid pgrp ...: comm may hold spaces/parens
                rest = f.read().rsplit(")", 1)[1].split()
            if int(rest[2]) == pgid:
                pids.append(int(name))
        except (OSError, IndexError, ValueError):
            continue
    return sorted(pids)[:5]


def _limits(cpu_seconds: int) -> None:
    # best-effort: a limit that can't apply must not kill the run — the VM is
    # the boundary, these just keep the one shared guest responsive.
    #
    # No RLIMIT_AS on purpose. It caps *virtual* address space, and V8 (node,
    # npm, and anything that embeds it — esbuild, vite) reserves far more
    # virtual memory than it ever commits; a 512MB AS cap made `npm` abort with
    # SIGABRT while bare `node` only just fit. On a fixed-RAM guest that cap
    # bought almost nothing beyond physical RAM anyway — real memory is bounded
    # by the guest's RAM ceiling + the OOM killer, which only touches this
    # disposable guest. CPU time is the responsiveness guard instead, and it
    # tracks the run's own wall-clock timeout (times a few cores of headroom) so
    # a legitimate long build isn't SIGXCPU'd early while a runaway still can't
    # outlast its deadline.
    for limit, val in (
        (resource.RLIMIT_CPU, cpu_seconds),
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
        if SKIP_DIRS.intersection(rel.parts):
            continue
        if p.is_file():
            st = p.stat()
            snap[str(rel)] = (st.st_mtime_ns, st.st_size)
    return snap


def _sync_overlay(root) -> set:
    """Lay this turn's pending writes (write_file/edit_file buffer them in
    `.staging/`) over the workspace copy, so the code sees the files the agent
    just wrote. Without it `node --test tests/new.test.mjs` said the file did
    not exist (2026-09-27 plan run) and agents went digging in .staging by hand.
    Runs before the `before` snapshot, so synced files are not re-captured; the
    overlay itself is untouched and still what the turn-end pack ships."""
    overlay = root / ".staging"
    laid: set = set()
    if not overlay.is_dir():
        return laid
    for src in overlay.rglob("*"):
        if not src.is_file() or src.is_symlink():
            continue
        dest = root / src.relative_to(overlay)
        if dest.is_symlink() or dest.is_dir():
            continue
        laid.add(src.relative_to(overlay))
        data = src.read_bytes()
        if dest.is_file() and dest.read_bytes() == data:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return laid


def _drop_removed_overlay(root, laid: set) -> None:
    """A staged file the run then removed (`rm` on something write_file/edit_file
    had buffered) is gone for good: its overlay copy would otherwise be shipped
    at turn end as a write and the file would come back. Only files this run's
    sync laid down are considered, so the buffer's own bookkeeping is untouched."""
    overlay = root / ".staging"
    for rel in laid:
        if (root / rel).exists() or (root / rel).is_symlink():
            continue
        try:
            (overlay / rel).unlink()
        except OSError:
            continue
        parent = (overlay / rel).parent
        while parent != overlay:                  # prune the directories it emptied
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

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


_URL_HOST = re.compile(r"(?:https?|ssh|git)://(?:[^@/\s'\"]*@)?([^/\s:'\"?#\]]+|\[[0-9a-f:]+\])",
                       re.I)
_SCP_HOST = re.compile(r"\bgit@([A-Za-z0-9.-]+):")
_BARE_IP = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _hosts_in(text: str) -> list[str]:
    """Hosts a failed command named (URLs, git@host:, bare IPv4), in order."""
    found: list[str] = []
    for rx in (_URL_HOST, _SCP_HOST, _BARE_IP):
        for m in rx.finditer(text or ""):
            h = m.group(1).strip("[]").lower().rstrip(".")
            if h and h not in found:
                found.append(h)
    return found


def _refused_outright(host: str) -> bool:
    """Targets the egress proxy refuses for every box before any policy and
    never queues: this machine's own address, loopback, private/LAN and
    link-local (cloud metadata) space, and names that are not on the internet."""
    proxy = os.environ.get("JARVIS_EGRESS_PROXY", "")
    m = re.search(r"//([^:/]+)", proxy)
    if m and host == m.group(1).lower():
        return True                                   # the host itself
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return (host == "localhost" or "." not in host
                or host.endswith((".local", ".localdomain", ".internal", ".lan", ".home.arpa")))
    return not ip.is_global


def _egress_note(source: str, output: str) -> str:
    """What to tell the model after a failed network call under monitored
    egress. The proxy answers every refusal with the same bare 403, and only an
    UNDECIDED host (deny-by-default policy) is queued for the operator: a host
    on a deny list, a profile with the network off, and the host/loopback/LAN/
    metadata addresses are refused and NOT queued. This handler cannot see which
    happened, so it says what the named hosts can only be, never that anything
    was queued (it used to say 'QUEUED' for all of them)."""
    hosts = _hosts_in(source + "\n" + output)
    refused = [h for h in hosts if _refused_outright(h)]
    public = [h for h in hosts if h not in refused]
    parts = []
    if refused:
        parts.append(
            f"[network refused: {', '.join(refused[:4])} is this machine's host, "
            "loopback, or a private/LAN/metadata address. The egress proxy never "
            "forwards there and does not queue it, so there is nothing for the "
            "operator to approve; do not ask. A repository on the host (the "
            "project's Gitea) is reached only through git_commit_request / "
            "git_push_request, not from the sandbox.]")
    if public or not refused:
        named = (f"{', '.join(public[:4])}: " if public else "")
        parts.append(
            f"[network blocked: {named}the VM has monitored egress ON and the "
            "proxy refused this call. If the project's policy is deny-by-default "
            "and the host is undecided, the proxy queues it in the Network tab "
            "for the operator; a host on a deny list, or a profile with the "
            "network off, is refused and not queued. The Network tab shows which. "
            "Name the exact hosts you need, ask the operator to check it, then "
            "re-run this command.]")
    return "\n".join(parts)


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
        laid = _sync_overlay(cwd)
        before = _snapshot(cwd)
    else:
        laid = set()
        cwd = settings.projects_dir / "_scratch"
        cwd.mkdir(parents=True, exist_ok=True)
        before = None               # no project: nothing to stage artifacts into

    argv = (["python3", "-c", code] if code else ["/bin/sh", "-c", command])
    script = None
    if command:
        # Run the command from a script file, not `sh -c <command>`: with -c the
        # whole text sits in the shell's own cmdline, so `pkill -f <text from
        # it>` matched and killed the shell itself (exit -15, ten times across
        # three plan runs; PLANS-10). The shell reads it as it runs, so it is
        # removed as soon as the shell has exited.
        try:
            fd, script = tempfile.mkstemp(prefix="rc-", suffix=".sh", dir="/tmp")
            with os.fdopen(fd, "w") as f:
                f.write(command + "\n")
            argv = ["/bin/sh", script]
        except OSError:
            script = None               # fall back to -c
    path = "/usr/local/bin:/usr/bin:/bin"
    if os.path.isdir(PIP_VENV_BIN):
        # an image variant with approved pip packages: its venv comes first
        # (absent on the main image, so PATH there is exactly as before)
        path = f"{PIP_VENV_BIN}:{path}"
    env = {"PATH": path, "HOME": str(cwd),
           "PYTHONUNBUFFERED": "1", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           # Package-manager caches/scratch go to /tmp, never the project copy,
           # so they don't ride the turn-end reconcile back as artifacts.
           "npm_config_cache": "/tmp/.npm", "XDG_CACHE_HOME": "/tmp/.cache",
           "npm_config_update_notifier": "false", "npm_config_fund": "false"}
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
                   NO_PROXY=_local, no_proxy=_local,
                   # a refused host answers 403 every time: pip's default five
                   # retries only cost 10-18 s per attempt (BUILD-05)
                   PIP_RETRIES="1")
    # CPU-seconds backstop = a few cores busy for the whole wall window, plus
    # headroom; the wall-clock SIGKILL below is the real deadline.
    cpu_cap = timeout * 4 + 30
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=env,
            preexec_fn=functools.partial(_limits, cpu_cap),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL)
    except OSError as e:
        if script:
            try:
                os.unlink(script)
            except OSError:
                pass
        return f"error: could not start the process: {e}"

    # Drain the pipes into buffers with our OWN tasks and watch for the SHELL's
    # exit ourselves. proc.wait() also waits for the pipes to close, and a
    # background process the command left behind (`server &`, output not
    # redirected) holds them open: the call hung until that process died and
    # the timeout could not rescue it (PLANS-01). On timeout we SIGKILL the
    # group (pgid == pid, _limits() setsid()s; getpgid() on a reaped shell
    # raised and skipped the kill). Once the shell is gone the pipes get a short
    # grace, then we detach from them, still reading into the void so the
    # background process never blocks on a full pipe. Output emitted before the
    # kill or the detach is kept.
    bufs: dict[str, list] = {"out": [], "err": []}

    async def drain(stream, key: str) -> None:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            if bufs[key] is not None:
                bufs[key].append(chunk)
    tasks = {asyncio.ensure_future(drain(proc.stdout, "out")),
             asyncio.ensure_future(drain(proc.stderr, "err"))}

    timed_out = False
    delay = 0.005
    while proc.returncode is None:
        if time.monotonic() - t0 >= timeout:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            break
        await asyncio.sleep(delay)
        delay = min(delay * 2, 0.1)
    if script:
        try:
            os.unlink(script)
        except OSError:
            pass
    _done, pending = await asyncio.wait(tasks, timeout=PIPE_GRACE)
    detached = ""
    if pending:
        out_b, err_b = b"".join(bufs["out"]), b"".join(bufs["err"])
        bufs["out"] = bufs["err"] = None          # keep reading, keep nothing
        pids = _group_pids(proc.pid)
        detached = ("(background process" + (f" {', '.join(map(str, pids))}" if pids else "")
                    + " from this command is still running; its output is detached. "
                    "Redirect the output when you use &: `cmd > /tmp/x.log 2>&1 &`, "
                    "and kill it when you are done)")
    else:
        out_b, err_b = b"".join(bufs["out"]), b"".join(bufs["err"])
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
    if detached:
        lines.append(detached)

    # network failures here are almost always the monitored-egress gate, not a
    # permanent wall — surface the fix instead of letting the model give up.
    combined = (out + "\n" + err).lower()
    net_markers = ("temporary failure resolving", "could not resolve host",
                   "name or service not known", "network is unreachable",
                   "connection refused", "failed to establish a new connection",
                   "no route to host", "proxyerror", "connection timed out",
                   "could not resolve proxy", "tunnel connection failed",
                   "connect tunnel failed", "no matching distribution found",
                   "could not find a version that satisfies")
    # `pip install ... | tail`, `cmd; echo done` and a curl that prints only its
    # tunnel error all exit 0 and got no hint (PLANS-06 / BUILD-05). A clean exit
    # gets it only for the markers that name the proxy or a package that could
    # not be fetched; a mere "connection refused" in a passing test's output does not.
    strong = ("proxyerror", "tunnel connection failed", "connect tunnel failed",
              "no matching distribution found", "could not find a version that satisfies")
    proxy_on = bool(os.environ.get("JARVIS_EGRESS_PROXY"))
    if any(m in combined for m in (net_markers if proc.returncode != 0 else strong)):
        if proxy_on:
            lines.append(_egress_note(code or command, out + "\n" + err))
        else:
            lines.append(
                "[network blocked: the VM has no internet right now (monitored egress "
                "is OFF). Name the exact hosts this needs (e.g. github.com, pypi.org) "
                "and tell the operator to enable egress + approve them in the Network "
                "tab. Do NOT just conclude the sandbox has no network and stop.]")

    if before is not None:
        _drop_removed_overlay(cwd, laid)
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
