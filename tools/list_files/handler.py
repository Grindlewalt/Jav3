from backend.config import settings
from backend.fsutil import list_tree
from backend.writes import pending_paths
from backend.agent.tools.toolctx import require_project


async def run() -> str:
    slug = await require_project()
    files = list_tree(settings.projects_dir / slug)
    # in-guest: files this turn wrote that haven't synced back to the host yet;
    # host-side writes apply immediately, so this is empty there
    pending = pending_paths(slug)
    lines = [f"project: {slug}"]
    listed = set()
    for f in files:
        listed.add(f["path"])
        mark = " (edited this turn)" if f["path"] in pending else ""
        lines.append(f"{f['path']}  [{f['size']}B]{mark}")
    for path, status in pending.items():
        if path not in listed:
            lines.append(f"{path}  (new this turn)")
    return "\n".join(lines) if len(lines) > 1 else f"project {slug} has no files"
