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
    if r.get("secret_files"):
        parts.append("warning: a secret VALUE was written into staged file(s): "
                     + ", ".join(r["secret_files"])
                     + " — tell the operator to review before approving")
    return "\n".join(parts)


def _has_executable(code: str) -> bool:
    return any(s.strip() and not s.strip().startswith("#")
               for s in code.splitlines())


async def run(code: str, timeout: float | None = None) -> str:
    # convo-16 failure mode: the model used run_code as a notepad, submitting
    # comment-only snippets that boot a VM round-trip to print nothing
    if not _has_executable(code or ""):
        return ("error: this code contains no executable statements — run_code "
                "executes Python in the sandbox VM, it is not a notepad. Put "
                "reasoning in your reply text; call run_code only to actually "
                "compute something.")
    slug = await require_project()
    try:
        return _fmt(await run_in_project(slug, "python3 -", timeout=timeout, input=code))
    except VMError as e:
        return f"error: sandbox VM problem: {e}"
