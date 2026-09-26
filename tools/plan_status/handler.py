from backend import plan as plan_mod
from backend import runtime
from backend.agent.tools.toolctx import require_project


async def run(wait_seconds: int = 0) -> str:
    slug = await require_project()
    # the caller's own conversation, restored host-side by broker_dispatch: the
    # wait ends early when a message for it is queued
    return await plan_mod.status(slug, wait_seconds=wait_seconds,
                                 cid=runtime.conversation_id.get())
