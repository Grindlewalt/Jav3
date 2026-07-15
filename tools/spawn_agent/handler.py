from backend import agents_run
from backend.agent.budget import current as current_budget
from backend.db import get_db


async def run(agent: str, task: str) -> str:
    from fastapi import HTTPException
    b = current_budget()
    before = (b.input_tokens + b.output_tokens) if b else None
    try:
        result = await agents_run.run_agent_headless(agent, task)
    except HTTPException as e:
        if e.status_code == 404:
            return (f"error: no agent named '{agent}'. Check the agent list — "
                    "agents are created in the Agents tab.")
        return f"error: {e.detail}"
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
