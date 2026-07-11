from backend.egress import file_request
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
    row = await file_request(slug, str(host).strip(), port, reason=reason or None)
    return (f"egress request #{row['id']} filed for {host}:{port} — status "
            f"{row['status']}. The sandbox stays blocked until the operator "
            "approves it in the dashboard; continue with other work meanwhile.")
