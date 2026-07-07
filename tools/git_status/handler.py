from backend.agent.tools.toolctx import require_project
from backend.gitgate import ensure_repo, status_text


async def run() -> str:
    try:
        slug = await require_project()
    except LookupError as e:
        return f"error: {e}"
    await ensure_repo(slug)
    return await status_text(slug)
