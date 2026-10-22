from backend.agent.tools.toolctx import require_project
from backend.research import result_text, run_research


async def run(topic: str, angles: int = 4) -> str:
    project = await require_project()
    r = await run_research(topic, project, n_angles=angles)
    return await result_text(project, r)
