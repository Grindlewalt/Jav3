from backend.agent.tools.toolctx import require_project
from backend.staging import stage_write


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
    stage_write(slug, path, html.encode())  # safe_join inside re-checks escapes
    return (f"staged dashboard at {path} ({len(html)} chars) — pending operator "
            "approval. Once approved it renders in the project workspace "
            "Renderer panel (sandboxed iframe: scripts run, but no network and "
            "no same-origin access).")
