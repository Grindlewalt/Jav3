from backend.agent.tools.toolctx import require_project
from backend.gitgate import diff_text, ensure_repo


async def run(path: str | None = None) -> str:
    try:
        slug = await require_project()
    except LookupError as e:
        return f"error: {e}"
    await ensure_repo(slug)
    return await diff_text(slug, path)
