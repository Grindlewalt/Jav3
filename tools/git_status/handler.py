from backend.agent.tools.toolctx import require_project
from backend.gitgate import ensure_repo, flush_guest_writes, status_text


async def run() -> str:
    try:
        slug = await require_project()
    except LookupError as e:
        return f"error: {e}"
    await ensure_repo(slug)
    await flush_guest_writes(slug)      # this turn's writes are still in the VM
    return await status_text(slug)
