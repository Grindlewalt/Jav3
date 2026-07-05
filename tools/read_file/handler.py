from backend.staging import effective_read
from backend.agent.tools.toolctx import require_project

MAX = 100_000


async def run(path: str) -> str:
    slug = await require_project()
    p = effective_read(slug, path)
    if p is None:
        return f"error: no such file: {path}"
    try:
        text = p.read_text()
    except UnicodeDecodeError:
        return f"error: {path} is binary ({p.stat().st_size} bytes)"
    if len(text) > MAX:
        return text[:MAX] + f"\n...(truncated, {len(text)} chars total)"
    return text or "(empty file)"
