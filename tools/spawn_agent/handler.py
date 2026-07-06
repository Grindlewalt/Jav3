from backend.agents_run import run_agent_headless


async def run(agent: str, task: str) -> str:
    from fastapi import HTTPException
    try:
        result = await run_agent_headless(agent, task)
    except HTTPException as e:
        if e.status_code == 404:
            return (f"error: no agent named '{agent}'. Check the agent list — "
                    "agents are created in the Agents tab.")
        return f"error: {e.detail}"
    return f"[{result['agent']} reports]\n{result['final']}"
