from backend.agent.tools.toolctx import require_project
from backend.agent.tools import vm
from backend.gate import run_gated


async def run(command: str, timeout: float | None = None, fresh: bool = True) -> str:
    if not command.strip():
        return "error: empty command"
    try:
        slug = await require_project()
    except LookupError as e:
        return f"error: {e}"
    try:
        r = await run_gated(slug, command, timeout=timeout, fresh=fresh)
    except (vm.VMError, LookupError) as e:
        return f"error: {e}"
    lines = [
        f"gate run {r['run_id']}: exit={r['exit_status']} status={r['status']}",
        f"egress lock: {'verified' if r['egress_locked'] else 'NOT VERIFIED'}",
        f"dns lookups: {r['dns_lookups']}, blocked egress attempts: "
        f"{r['blocked_attempts']}, execs logged: {r['execs_logged']}",
        f"staged: {', '.join(r['staged']) if r['staged'] else 'none'}",
        f"report staged at {r['report']} — operator must review it and approve "
        "staged files before anything goes live",
    ]
    if r.get("error"):
        lines.insert(1, f"error: {r['error']}")
    return "\n".join(lines)
