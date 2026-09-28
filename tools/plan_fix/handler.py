from backend import plan as plan_mod
from backend.agent.tools.toolctx import require_project


async def run(action: str, item: str = "", guidance: str = "", title: str = "",
              brief: str = "", depends_on: list | None = None, run: bool = True) -> str:
    slug = await require_project()
    return await plan_mod.fix(slug, action=action, item=item or None, guidance=guidance,
                              title=title, brief=brief, depends_on=depends_on, run=run)
