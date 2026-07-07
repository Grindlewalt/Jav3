"""Running an agent = a ReAct turn with the agent's own system prompt and a
tool set trimmed by its exclusions, streamed and persisted like a chat so the
run is findable afterward. This is the concrete implementation behind the
Agents tab's definitions; the operator kicks one off from the project board.

The agent runs in the ACTIVE PROJECT: it gets the project's assembled context
(minus any context items the agent excludes) and the same staged-write tools,
so its file changes land in the approval queue exactly like Jarvis's own.
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .agent.loop import run_turn
from .agent.model import confirm_peak, in_peak_window, peak_confirmed
from .agent.tools.registry import load_registry, openai_tool_specs
from .agents_api import _read
from .auth import require_user
from .config import settings
from .db import get_db
from .memory import assemble_system_prompt, get_active_project

router = APIRouter(prefix="/api/agents", tags=["agents"],
                   dependencies=[Depends(require_user)])


class RunAgent(BaseModel):
    task: str
    confirm_peak: bool = False


def sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _agent_overrides(agent: dict) -> tuple[str | None, str | None]:
    """(model_name, base_url) for this agent — empty means inherit the default."""
    return (agent.get("model") or None, agent.get("base_url") or None)


def _agent_tools(agent: dict) -> list[dict]:
    excluded = set(agent.get("tools_exclude") or [])
    # an agent never spawns further agents — no recursion, no fork bombs
    excluded.add("spawn_agent")
    entries = [e for e in load_registry() if e["name"] not in excluded]
    return openai_tool_specs(entries)


_USE_DB = object()


async def _agent_system_prompt(db, agent: dict, active=_USE_DB) -> str:
    """The agent's prompt, then the shared project context minus excluded
    sections. The agent's context_exclude tokens (soul.md, user.md, env.md,
    all-projects.md, active-project, ...) are assemble_system_prompt's block
    labels, so exclusion happens at assembly instead of post-hoc splitting."""
    exclude = set(agent.get("context_exclude") or [])
    base = (await assemble_system_prompt(db, exclude=exclude) if active is _USE_DB
            else await assemble_system_prompt(db, active=active, exclude=exclude))
    return f"{agent['prompt']}\n\n---\n\n{base}"


async def _open_agent_run(db, slug: str, task: str, active=_USE_DB) -> tuple[dict, int]:
    """Create the conversation for an agent run and record the task. Returns
    (agent def, conversation_id)."""
    agent = _read(slug)  # 404s if missing
    if active is _USE_DB:
        active = await get_active_project(db)
    project_id = None
    if active:
        async with db.execute(
            "SELECT id FROM projects WHERE slug = ?", (active,)) as cur:
            row = await cur.fetchone()
        project_id = row["id"] if row else None
    title = f"[{agent['name']}] " + " ".join(task.split())[:40]
    cur = await db.execute(
        "INSERT INTO conversations (project_id, summary, kind) VALUES (?, ?, 'agent')",
        (project_id, title))
    conversation_id = cur.lastrowid
    await db.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
        (conversation_id, task))
    await db.commit()
    return agent, conversation_id


async def run_agent_headless(slug: str, task: str, active=_USE_DB) -> dict:
    """Run an agent to completion, no streaming — for scheduled runs and the
    spawn_agent tool. Peak is auto-confirmed: the caller (a schedule or Jarvis
    itself) already intended this, there's no human to prompt. `active` pins
    the project context without disturbing the operator's live session."""
    db = await get_db()
    try:
        agent, conversation_id = await _open_agent_run(db, slug, task, active=active)
        confirm_peak(conversation_id)
        system_prompt = await _agent_system_prompt(db, agent, active=active)
        tools = _agent_tools(agent)
        mdl, burl = _agent_overrides(agent)
        history = [{"role": "user", "content": task}]
        final_content = ""
        async for event in run_turn(db, conversation_id, system_prompt,
                                    history, tools=tools, model_name=mdl, base_url=burl):
            if event["type"] == "final":
                final_content = event["content"]
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) "
            "VALUES (?, 'assistant', ?)", (conversation_id, final_content))
        await db.commit()
        return {"conversation_id": conversation_id, "agent": agent["name"],
                "final": final_content}
    finally:
        await db.close()


@router.post("/{slug}/run")
async def run_agent(slug: str, body: RunAgent):
    agent = _read(slug)  # 404s if missing
    db = await get_db()
    try:
        active = await get_active_project(db)
        project_id = None
        if active:
            async with db.execute(
                "SELECT id FROM projects WHERE slug = ?", (active,)) as cur:
                row = await cur.fetchone()
            project_id = row["id"] if row else None
        title = f"[{agent['name']}] " + " ".join(body.task.split())[:40]
        cur = await db.execute(
            "INSERT INTO conversations (project_id, summary, kind) VALUES (?, ?, 'agent')",
            (project_id, title))
        conversation_id = cur.lastrowid
        await db.commit()

        if body.confirm_peak:
            confirm_peak(conversation_id)
        if in_peak_window() and not peak_confirmed(conversation_id):
            raise HTTPException(
                status_code=409, detail="peak_confirmation_required",
                headers={"X-Conversation-Id": str(conversation_id)})

        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
            (conversation_id, body.task))
        await db.commit()
    finally:
        await db.close()

    async def event_stream():
        db = await get_db()
        try:
            yield sse({"type": "start", "conversation_id": conversation_id,
                       "agent": agent["name"]})
            system_prompt = await _agent_system_prompt(db, agent)
            tools = _agent_tools(agent)
            mdl, burl = _agent_overrides(agent)
            history = [{"role": "user", "content": body.task}]
            final_content = ""
            async for event in run_turn(db, conversation_id, system_prompt,
                                        history, tools=tools, model_name=mdl, base_url=burl):
                if event["type"] == "final":
                    final_content = event["content"]
                else:
                    yield sse(event)
            await db.execute(
                "INSERT INTO messages (conversation_id, role, content) "
                "VALUES (?, 'assistant', ?)", (conversation_id, final_content))
            await db.commit()
            yield sse({"type": "final", "content": final_content})
        except Exception as e:  # noqa: BLE001 — surface to the GUI, don't 500 mid-stream
            yield sse({"type": "error", "message": str(e)})
        finally:
            await db.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")
