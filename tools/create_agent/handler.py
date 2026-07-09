"""Create an agent definition (agents/<slug>/AGENT.md) via the same writer the
Agents tab uses, so GUI edits and self-created agents are the same data. An
agent definition alone executes nothing — it only runs when spawned or when
the operator approves a schedule for it — so creation is live, not staged."""
from backend import runtime
from backend.agents_api import SaveAgent, _agent_path, _slugify, _write


async def run(name: str, prompt: str, description: str = "") -> str:
    if runtime.ephemeral.get():
        return "error: not available in incognito chat — agents are durable."
    if not (name or "").strip() or not (prompt or "").strip():
        return "error: create_agent needs a name and a full system prompt."
    try:
        slug = _slugify(name)
    except Exception:  # HTTPException from a name with no usable characters
        return f"error: '{name}' produces an empty slug — use letters/digits."
    if _agent_path(slug).exists():
        return (f"error: agent '{slug}' already exists — spawn_agent it, or "
                "pick a different name.")
    _write(slug, SaveAgent(name=name.strip(), description=description.strip(),
                           prompt=prompt.strip()))
    return (f"created agent '{slug}' — it is in your roster now: run it "
            "with spawn_agent, or propose a recurring run with "
            "schedule_update. Tell the operator so they can review the prompt "
            "in the Agents tab.")
