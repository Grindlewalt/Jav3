from backend.config import settings
from backend.fsutil import list_tree
from backend.staging import list_staged
from backend.agent.tools.toolctx import require_project


async def run() -> str:
    slug = await require_project()
    files = list_tree(settings.projects_dir / slug)
    staged = {e["path"]: e["status"] for e in list_staged(slug)}
    lines = [f"project: {slug}"]
    for f in files:
        mark = " (staged)" if f["path"] in staged else ""
        lines.append(f"{f['path']}  [{f['size']}B]{mark}")
    for path, status in staged.items():
        if status == "new":
            lines.append(f"{path}  (staged, new)")
    return "\n".join(lines) if len(lines) > 1 else f"project {slug} has no files"
