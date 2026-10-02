import contextvars
import uuid

from backend.agent.tools.registry import load_registry, openai_tool_specs
from backend.agent.tools.toolctx import require_project
from backend.autonomy import NON_DELEGABLE
from backend.config import settings
from backend.orchestrator import run_job
from backend.vm import guest_turn

# belt for the NON_DELEGABLE suspenders: even if a spec leaks back in (a
# node falling through with tools=None gets the full registry), a worker
# can't deploy another team under itself — contextvars propagate into the
# job's task tree, making this a cheap whole-subtree recursion fence
_in_funnel = contextvars.ContextVar("jav3_in_funnel", default=False)


async def _put_rollups(slug: str, job_id: str) -> str:
    """The node rollups are written on the host; a guest turn's copy of the
    project was built at turn start, so put them in it (read_file would say
    "no such file"). "" when they are there or the turn runs on the host."""
    d = settings.projects_dir / slug / "runs" / job_id
    files = {f"runs/{job_id}/{p.name}": p.read_bytes()
             for p in sorted(d.glob("*.md"))[:40]} if d.is_dir() else {}
    if await guest_turn.push_files(slug, files) is False:
        return " (not in this turn's workspace yet: read_file will find them next turn)"
    return ""


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
            [e for e in load_registry() if e["name"] not in NON_DELEGABLE])
        r = await run_job(job_id, brief, slug, leaf_tools=leaf_tools,
                          title=(title or brief)[:60])
        return (f"Agent team finished (job {job_id}); node rollups staged "
                f"under runs/{job_id}/ for review{await _put_rollups(slug, job_id)}."
                f"\n\nRollup:\n{r['rollup']}")
    finally:
        _in_funnel.reset(token)
