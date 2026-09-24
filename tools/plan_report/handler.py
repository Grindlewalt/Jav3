from backend import plan as plan_mod
from backend import runtime
from backend.agent.tools.toolctx import require_project


async def run(status: str, summary: str, item: str | None = None) -> str:
    slug = await require_project()
    return await plan_mod.report(slug, cid=runtime.conversation_id.get(),
                                 item_id=(item or "").strip() or None,
                                 status=(status or "").strip().lower(), summary=summary)
