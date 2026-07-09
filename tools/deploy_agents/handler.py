import contextvars
import uuid

from backend.agent.tools.toolctx import require_project
from backend.orchestrator import run_job

# a funnel's own subagents run with the full registry, so without this guard a
# worker could deploy another team under itself — contextvars propagate into
# the job's task tree, making this a cheap whole-subtree recursion fence
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
        r = await run_job(job_id, brief, slug, peak=True,
                          title=(title or brief)[:60])
        return (f"Agent team finished (job {job_id}); node rollups staged "
                f"under runs/{job_id}/ for review.\n\nRollup:\n{r['rollup']}")
    finally:
        _in_funnel.reset(token)
