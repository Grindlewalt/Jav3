from backend import plan as plan_mod
from backend import runtime
from backend.agent.tools.toolctx import require_project
from backend.writes import SecretLeakError


async def run(dump: str, files: list[str] | None = None, run: bool = True,
              models: list[dict] | None = None) -> str:
    if runtime.ephemeral.get():
        # a plan is a persisted file and a team of recorded runs: exactly what
        # an incognito turn promises not to leave behind
        return "error: orchestrate is unavailable in an incognito chat — nothing it does would stay incognito."
    slug = await require_project()
    try:
        # validated before the planner runs: a model the operator named that
        # cannot run is theirs to hear about, not the default's to replace
        assigned = plan_mod.checked_models(models)
    except ValueError as e:
        return f"error: {e}. Nothing was planned."
    try:
        plan = await plan_mod.plan_from_dump(slug, dump, files or [], models=assigned)
    except SecretLeakError as e:
        return f"error: refused — the dump contains a secret value ({e}). Remove it and retry."
    except (ValueError, RuntimeError) as e:
        return f"error: {e}"
    out = [f"Plan '{plan['title']}' — {len(plan['items'])} items saved to "
           f"{slug}/.plan.json:", plan_mod.render_checklist(plan)]
    if not run:
        out.append("Not started (run=false): the operator can edit it on the Plan panel "
                   "and press Run there.")
        return "\n".join(out)
    try:
        # peak is the launching turn's decision: it already passed the gate
        started = await plan_mod.start_run(slug, peak=True)
    except RuntimeError as e:
        out.append(f"Not started: {e}")
        return "\n".join(out)
    out.append(f"Run started (job {started['job_id']}, head conversation "
               f"{started['root_id']}). It runs detached — tell the operator to follow "
               "it on the project's Plan panel; do not wait for it in this turn.")
    return "\n".join(out)
