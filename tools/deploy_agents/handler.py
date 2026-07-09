import contextvars
import uuid

from backend.agent.tools.registry import load_registry, openai_tool_specs
from backend.agent.tools.toolctx import require_project
from backend.orchestrator import run_job

# team workers never spawn further agents/teams and never mint persistent
# infrastructure (new agents, schedule proposals) — those stay decisions of
# the conversation head that deployed them
_WORKER_EXCLUDE = frozenset({"spawn_agent", "deploy_agents",
                             "create_agent", "schedule_update"})

# belt for the _WORKER_EXCLUDE suspenders: even if a spec leaks back in (a
# node falling through with tools=None gets the full registry), a worker
# can't deploy another team under itself — contextvars propagate into the
# job's task tree, making this a cheap whole-subtree recursion fence
_in_funnel = contextvars.ContextVar("jarvis_in_funnel", default=False)


async def run(brief: str, title: str = "") -> str:
    if not (brief or "").strip():
        return "error: empty brief — describe what the team should accomplish."
    if _in_funnel.get():
        return ("error: you are already part of a deployed team — do your own "
                "task directly instead of deploying another team.")
    slug = await require_project()
    token = _in_funnel.set(True)
    try:
        job_id = uuid.uuid4().hex
        leaf_tools = openai_tool_specs(
            [e for e in load_registry() if e["name"] not in _WORKER_EXCLUDE])
        r = await run_job(job_id, brief, slug, peak=True, leaf_tools=leaf_tools,
                          title=(title or brief)[:60])
        return (f"Agent team finished (job {job_id}); node rollups staged "
                f"under runs/{job_id}/ for review.\n\nRollup:\n{r['rollup']}")
    finally:
        _in_funnel.reset(token)
