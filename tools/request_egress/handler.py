from backend.egress import file_request, wait_for_decision
from backend.agent.tools.toolctx import require_project


async def run(host: str, port: int, reason: str = "") -> str:
    try:
        slug = await require_project()
    except LookupError as e:
        return f"error: {e}"
    if not host or not str(host).strip():
        return "error: host is required"
    try:
        port = int(port)
    except (TypeError, ValueError):
        return "error: port must be a number"
    host = str(host).strip()
    row = await file_request(slug, host, port, reason=reason or None)
    # pause here until the operator decides (up to 5 min) — no tokens burn while
    # waiting — then resume with the outcome.
    status = await wait_for_decision(row["id"], timeout=300)
    if status == "approved":
        return (f"approved — {host}:{port} is now on the sandbox allowlist. "
                "Retry the command that needed it.")
    if status == "denied":
        return (f"denied — do not use {host}:{port}. Find another way, or ask the "
                "operator what to do instead.")
    return (f"still pending after 5 min (request #{row['id']}). It will work once "
            "approved; continue with other work and retry later.")
