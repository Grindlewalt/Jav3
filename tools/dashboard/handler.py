from backend.agent.tools.toolctx import require_project
from backend.writes import SecretLeakError, apply_write


async def run(path: str, html: str) -> str:
    slug = await require_project()
    path = path.strip()
    if not path or path.startswith(("/", "\\")):
        return "error: path must be relative (no leading slash)"
    if not path.endswith(".html"):
        return "error: path must end in .html"
    parts = path.replace("\\", "/").split("/")
    if ".." in parts or "" in parts:
        return "error: path must not contain '..' or empty segments"
    if parts[0] != "dashboards":
        path = f"dashboards/{path}"
    try:
        await apply_write(slug, path, html.encode())  # safe_join inside re-checks escapes
    except SecretLeakError as e:
        return (f"error: dashboard refused — it contains the literal value of "
                f"secret(s): {', '.join(e.names)}. Never embed secret values.")
    return (f"dashboard written at {path} ({len(html)} chars). It renders in the "
            "project workspace Renderer panel (sandboxed iframe: scripts run, "
            "but no network and no same-origin access).")
