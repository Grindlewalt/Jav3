from backend.agents_run import compact_report, run_agent_headless


async def run(agent: str, task: str) -> str:
    from fastapi import HTTPException
    try:
        result = await run_agent_headless(agent, task)
    except HTTPException as e:
        if e.status_code == 404:
            return (f"error: no agent named '{agent}'. Check the agent list — "
                    "agents are created in the Agents tab.")
        return f"error: {e.detail}"
    # big reports get compacted: this string re-rides the parent's context
    # every remaining iteration of its turn
    final = await compact_report(result["agent"], task, result["final"],
                                 result["conversation_id"])
    return f"[{result['agent']} reports]\n{final}"
