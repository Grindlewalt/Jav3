"""Light execution sandbox for workspace runs on the Pi host.

This is NOT an agent execution sandbox — it's a convenience runner, driven
only by the operator from the Workspace Run panel: subprocess with
rlimits, a timeout, and process-group kill. Artifacts (plots, PDFs, anything
written to the project dir) are detected by mtime and surfaced to the GUI.
"""
import asyncio
import os
import resource
import signal
import time
from pathlib import Path

from .config import settings
from .fsutil import list_tree


def _limits():
    cpu = settings.run_timeout_seconds
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    mem = settings.run_max_mem_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024,) * 2)


async def run_python(project_dir: Path, script_rel: str) -> dict:
    script = project_dir / script_rel
    started = time.time()
    before = {f["path"] for f in list_tree(project_dir)}

    proc = await asyncio.create_subprocess_exec(
        settings.run_python, "-u", str(script),
        cwd=script.parent,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=lambda: (os.setsid(), _limits()),
        env={**os.environ, "MPLBACKEND": "Agg"},  # headless matplotlib
    )
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.run_timeout_seconds + 5)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = await proc.communicate()

    after = list_tree(project_dir)
    artifacts = [f["path"] for f in after
                 if f["path"] not in before or f["mtime"] >= started]
    artifacts = [a for a in artifacts if a != script_rel]

    cap = 100_000
    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "duration": round(time.time() - started, 2),
        "stdout": stdout.decode(errors="replace")[-cap:],
        "stderr": stderr.decode(errors="replace")[-cap:],
        "artifacts": artifacts,
    }
