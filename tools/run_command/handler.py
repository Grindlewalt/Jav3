from backend.agent.tools.vm import VMError
from backend.agent.tools.vmexec import run_in_project
from backend.agent.tools.toolctx import require_project


def _fmt(r: dict) -> str:
    parts = [f"exit {r['exit_status']}" + (" (timed out)" if r.get("timed_out") else "")]
    if r.get("stdout"):
        parts.append("stdout:\n" + r["stdout"][-8000:])
    if r.get("stderr"):
        parts.append("stderr:\n" + r["stderr"][-4000:])
    if not r.get("stdout") and not r.get("stderr"):
        parts[0] += " (no output)"
    if r.get("staged"):
        parts.append("files changed (staged for approval): " + ", ".join(r["staged"]))
    return "\n".join(parts)


async def run(command: str, timeout: float | None = None) -> str:
    if not (command or "").strip():
        return "error: empty command — pass the shell command to run."
    slug = await require_project()
    try:
        return _fmt(await run_in_project(slug, command, timeout=timeout))
    except VMError as e:
        return f"error: sandbox VM problem: {e}"
