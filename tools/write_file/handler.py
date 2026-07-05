from backend.staging import stage_write
from backend.agent.tools.toolctx import require_project


async def run(path: str, content: str) -> str:
    slug = await require_project()
    stage_write(slug, path, content.encode())
    return f"staged write to {path} ({len(content)} chars) — pending operator approval"
