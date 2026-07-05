from backend.staging import effective_read, stage_write
from backend.agent.tools.toolctx import require_project


async def run(path: str, find: str, replace: str, all: bool = False) -> str:
    slug = await require_project()
    p = effective_read(slug, path)
    if p is None:
        return f"error: no such file: {path}"
    text = p.read_text()
    count = text.count(find)
    if count == 0:
        return "error: `find` text not found — read_file and match exactly"
    if count > 1 and not all:
        return f"error: `find` matches {count} places — make it unique or set all=true"
    new = text.replace(find, replace)
    stage_write(slug, path, new.encode())
    n = count if all else 1
    return f"staged edit to {path} ({n} replacement{'s' if n != 1 else ''}) — pending operator approval"
