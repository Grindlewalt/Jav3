from backend import agents_run, runtime
from backend.agent.budget import current as current_budget
from backend.db import get_db


async def run(task: str, prompt: str, duplicate: bool = False,
              label: str = "", model: str | None = None) -> str:
    if not (task or "").strip() or not (prompt or "").strip():
        return ("error: spawn_temp_agent needs both a task and a role prompt "
                "for the agent.")
    if model:
        # operator-named: refuse out loud rather than fall back to the default
        from backend import providers
        try:
            model = providers.checked(model)
        except providers.ProviderError as e:
            return f"error: {e}. The agent was not started."
    b = current_budget()
    before = (b.input_tokens + b.output_tokens) if b else None
    # one hop deeper for the child's whole run, same fork-bomb fence as
    # spawn_agent: its toolset drops the spawn tools at the cap
    depth_token = runtime.spawn_depth.set(runtime.spawn_depth.get() + 1)
    try:
        result = await agents_run.run_temp_agent_headless(
            prompt, task, duplicate=bool(duplicate), label=label or "",
            **({"model": model} if model else {}))
    finally:
        runtime.spawn_depth.reset(depth_token)
    db = await get_db()
    try:
        async with db.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE conversation_id = ?",
            (result["conversation_id"],)) as cur:
            n = (await cur.fetchone())[0]
    finally:
        await db.close()
    report = result["final"]
    if not report.strip():
        report = "(agent completed but returned no output.)"
    # big reports get compacted: this string re-rides the parent's context
    # every remaining iteration of its turn
    final = await agents_run.compact_report(result["agent"], task, report,
                                            result["conversation_id"])
    if b:
        used = (b.input_tokens + b.output_tokens) - before
        trailer = f"\n(usage: ~{used:,} tokens, {n} tool calls)"
    else:
        trailer = f"\n(usage: {n} tool calls)"
    return f"[{result['agent']} reports]\n{final}{trailer}"
