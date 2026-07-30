"""Running an agent = a ReAct turn with the agent's own system prompt and a
tool set trimmed by its exclusions, streamed and persisted like a chat so the
run is findable afterward. This is the concrete implementation behind the
Agents tab's definitions; the operator kicks one off from the project board.

The agent runs in the ACTIVE PROJECT: it gets the project's assembled context
(minus any context items the agent excludes) and the same staged-write tools,
so its file changes land in the approval queue exactly like Jarvis's own.
"""
import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import bus
from .agent.loop import db_tool_sink
from .agent.model import confirm_peak, in_peak_window, model, peak_confirmed
from .vm.turn import run_agent_turn
from .agent.tools.registry import load_registry, openai_tool_specs
from .agents_api import _read
from .auth import require_user
from .config import settings
from .db import get_db, open_conversation
from .memory import assemble_system_prompt, get_active_project

router = APIRouter(prefix="/api/agents", tags=["agents"],
                   dependencies=[Depends(require_user)])


class RunAgent(BaseModel):
    task: str
    confirm_peak: bool = False
    # run in THIS project (workspace agent panels pass their slug) instead of
    # whatever project happens to be globally active — several agents can then
    # work different projects at once.
    project: str | None = None


def sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


# In-flight interactive runs, keyed by conversation. As in chat.py this dict is
# both the "still running" flag and the strong reference that keeps the task
# alive once the HTTP connection that started it has gone away.
_active_runs: dict[int, asyncio.Task] = {}

# Completion notices for named-agent runs the OPERATOR started. Deliberately
# only this path: `spawn_agent`/`spawn_temp_agent` children and orchestrator
# funnel nodes finish constantly inside a turn the operator is already
# watching, and toasting those would bury the one they actually walked away
# from.
NOTICE_CHAN = "agent_notices"


def _chan(conversation_id: int) -> str:
    return f"agentrun:{conversation_id}"


def _human_secs(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    m, sec = divmod(int(s), 60)
    return f"{m}m {sec:02d}s"


def _agent_overrides(agent: dict) -> tuple[str | None, str | None]:
    """(model_name, base_url) for this agent — empty means inherit the default."""
    return (agent.get("model") or None, agent.get("base_url") or None)


def _agent_tools(agent: dict, autonomy_level: str | None = None) -> list[dict]:
    from . import autonomy, runtime
    own_exclude = set(agent.get("tools_exclude") or [])
    excluded = set(own_exclude)
    # a subagent never launches teams or mints persistent infrastructure —
    # but the spawn tools themselves nest up to MAX_SPAWN_DEPTH (fork-bomb
    # cap; the shared per-op Budget fences cost). The agent definition's own
    # exclusion still wins.
    excluded |= autonomy.NON_DELEGABLE
    if runtime.spawn_depth.get() < autonomy.MAX_SPAWN_DEPTH:
        for t in ("spawn_agent", "spawn_temp_agent"):
            if t not in own_exclude:
                excluded.discard(t)
    entries = [e for e in load_registry() if e["name"] not in excluded]
    # a headless run is the unattended case — honour the project's autonomy dial
    entries = autonomy.filter_entries(entries, autonomy_level)
    return openai_tool_specs(entries)


async def _project_autonomy(db, slug: str | None) -> str | None:
    if not slug:
        return None
    async with db.execute("SELECT autonomy FROM projects WHERE slug = ?",
                          (slug,)) as cur:
        row = await cur.fetchone()
    return row["autonomy"] if row else None


_USE_DB = object()


async def _inherited_or_global(db) -> str | None:
    """The project an agent run belongs to when the caller didn't pin one: the
    running operation's own pin (a project-bound chat that spawn_agent'd us),
    else the GUI's global active project."""
    from . import runtime
    pinned = runtime.active_project.get()
    if pinned is not runtime.ACTIVE_UNSET:
        return pinned
    return await get_active_project(db)


async def _validate_project(db, slug: str) -> None:
    async with db.execute(
        "SELECT 1 FROM projects WHERE slug = ? AND deleted_at IS NULL",
        (slug,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail=f"no such project: {slug}")


async def _agent_system_prompt(db, agent: dict, active=_USE_DB) -> str:
    """The agent's prompt, then the shared project context minus excluded
    sections. The agent's context_exclude tokens (soul.md, user.md, env.md,
    all-projects.md, active-project, ...) are assemble_system_prompt's block
    labels, so exclusion happens at assembly instead of post-hoc splitting."""
    exclude = set(agent.get("context_exclude") or [])
    base = (await assemble_system_prompt(db, exclude=exclude) if active is _USE_DB
            else await assemble_system_prompt(db, active=active, exclude=exclude))
    return f"{agent['prompt']}\n\n---\n\n{base}"


async def _open_run(db, agent: dict, task: str,
                    active=_USE_DB) -> tuple[int, str | None]:
    """Create the conversation for an agent run and record the task. Returns
    (conversation_id, resolved project slug) — the caller needs the resolved
    slug (not the _USE_DB sentinel) for the autonomy lookup."""
    if active is _USE_DB:
        active = await _inherited_or_global(db)
    title = f"[{agent['name']}] " + " ".join(task.split())[:40]
    conversation_id = await open_conversation(
        db, project=active, title=title, kind="agent", commit=False)
    await db.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
        (conversation_id, task))
    await db.commit()
    return conversation_id, active


async def run_agent_headless(slug: str, task: str, active=_USE_DB) -> dict:
    """Run a defined agent to completion, no streaming — for scheduled runs and
    the spawn_agent tool."""
    agent = _read(slug)  # 404s if missing
    return await _run_headless(agent, task, active)


# blocks a lean temp agent drops: Jarvis's identity, standing memory, the user
# profile and the rosters — the bulk that re-rides every iteration without
# helping a narrow worker. env.md, the active project and the operator-rules
# tail stay (the tail is non-excludable anyway).
TEMP_LEAN_EXCLUDE = ("soul.md", "standing-memory", "user.md",
                     "all-projects.md", "agents-index", "secrets-index")

TEMP_REPORT_BACK = """# Temporary agent
You exist only for this task; when you finish you are gone, and only two
things survive you: the memory note you write and the final report you
return. If you built or changed anything durable (files, code, config),
record it FIRST with memory_write — one note named after the task: WHAT you
built (exact paths), HOW to use or implement it, and any follow-ups. Pure
lookups skip the note. Your final reply goes to the agent that spawned you:
outcome first, no process narration."""


def _temp_agent_def(prompt: str, duplicate: bool, label: str = "") -> dict:
    """An in-memory AGENT.md equivalent — same keys the _agent_* helpers read,
    never touches the roster on disk. `duplicate` mirrors the operator's ask:
    a full copy of Jarvis's context only when the task truly needs it."""
    return {
        "name": (label or "").strip()[:40] or "temp agent",
        "prompt": prompt.strip() + "\n\n" + TEMP_REPORT_BACK,
        "description": "", "model": "", "base_url": "", "own_memory": False,
        "context_exclude": [] if duplicate else list(TEMP_LEAN_EXCLUDE),
        "tools_exclude": [], "skills_exclude": [], "max_iterations": 0,
    }


async def run_temp_agent_headless(prompt: str, task: str, *,
                                  duplicate: bool = False, label: str = "",
                                  active=_USE_DB) -> dict:
    """A disposable agent: no AGENT.md, no roster entry — a role prompt layered
    on Jarvis's own context (full when duplicate, lean otherwise), run once and
    gone. What survives is the run's conversation row (Jobs view) and any
    memory note the agent writes."""
    return await _run_headless(_temp_agent_def(prompt, duplicate, label),
                               task, active)


async def _run_headless(agent: dict, task: str, active=_USE_DB) -> dict:
    """Shared engine for named and temp headless runs. Peak is auto-confirmed:
    the caller (a schedule or Jarvis itself) already intended this, there's no
    human to prompt. `active` pins the project context without disturbing the
    operator's live session.

    Headless runs are subagents of something (a parent turn or a schedule), so
    they get the tight subagent iteration cap unless the agent's definition
    grants more via max_iterations — the full 40-round chat cap is what let a
    subagent read dozens of pages and snowball its context."""
    from . import runtime
    db = await get_db()
    try:
        # take the RESOLVED slug back: _project_autonomy below binds `active`
        # as an SQL parameter, and the raw _USE_DB object() crashes aiosqlite
        conversation_id, active = await _open_run(db, agent, task, active=active)
        # own fetch-ledger scope: the agent hasn't seen its parent's reads, so
        # it must be able to re-fetch them — and a scheduled run must never be
        # starved by yesterday's claims (the 06:45 news-agent post-mortem)
        wtoken = runtime.web_session.set(f"run:{conversation_id}")
        # pin the run's project for its tools (host loop path) + its own children
        ptoken = runtime.active_project.set(active)
        cidtoken = runtime.conversation_id.set(conversation_id)
        confirm_peak(conversation_id)
        system_prompt = await _agent_system_prompt(db, agent, active=active)
        tools = _agent_tools(agent, await _project_autonomy(db, active))
        mdl, burl = _agent_overrides(agent)
        cap = agent.get("max_iterations") or settings.subagent_max_iterations
        history = [{"role": "user", "content": task}]
        final_content = ""
        try:
            async for event in run_agent_turn(conversation_id, system_prompt, history,
                                              tools=tools, model_name=mdl,
                                              base_url=burl, max_iterations=cap,
                                              active_project=active,
                                              on_tool_call=db_tool_sink(db, conversation_id)):
                if event["type"] == "final":
                    final_content = event["content"]
        finally:
            runtime.conversation_id.reset(cidtoken)
            runtime.active_project.reset(ptoken)
            runtime.web_session.reset(wtoken)
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) "
            "VALUES (?, 'assistant', ?)", (conversation_id, final_content))
        await db.commit()
        return {"conversation_id": conversation_id, "agent": agent["name"],
                "final": final_content}
    finally:
        await db.close()


async def compact_report(agent_name: str, task: str, report: str,
                          conversation_id: int) -> str:
    """A spawned agent's report becomes a tool result in the PARENT's loop and
    re-rides its context every remaining iteration, so a big one is compacted
    to a tight summary first (the full report stays persisted on the agent's
    conversation, findable in the Jobs view). Falls back to plain truncation if
    the summarize call fails — compaction must never lose the run."""
    cap = settings.agent_report_max_chars
    if len(report) <= cap:
        return report
    try:
        parts = []
        async for ev in model.complete([
            {"role": "system", "content":
                "Compress this agent report for the agent that requested it: "
                "keep every finding, decision, number and file path that the "
                "requester needs; drop process narration. Tight markdown, no "
                "preamble."},
            {"role": "user", "content": f"Task: {task}\n\nReport:\n{report[:24_000]}"},
        ], temperature=0.2):
            if ev["type"] == "message":
                parts.append(ev["content"])
        summary = "".join(parts).strip()
        if not summary:
            raise ValueError("empty summary")
        return (f"{summary}\n\n(compacted from {len(report):,} chars — full "
                f"report on conversation {conversation_id} in the Jobs view)")
    except Exception:  # noqa: BLE001 — degrade to truncation, never fail the run
        return (report[:cap] + f"\n...(truncated: {len(report):,} chars total — "
                f"full report on conversation {conversation_id} in the Jobs view)")


@router.post("/{slug}/run")
async def run_agent(slug: str, body: RunAgent):
    agent = _read(slug)  # 404s if missing
    db = await get_db()
    try:
        if body.project:
            await _validate_project(db, body.project)
            active = body.project
        else:
            active = await get_active_project(db)
        title = f"[{agent['name']}] " + " ".join(body.task.split())[:40]
        conversation_id = await open_conversation(
            db, project=active, title=title, kind="agent")

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

    # subscribe BEFORE spawning so this tail cannot miss the first events, then
    # detach the run: closing the panel or leaving the page used to cancel the
    # response generator and take the agent's work down with it
    q = bus.subscribe(_chan(conversation_id))
    _active_runs[conversation_id] = asyncio.create_task(
        _run_interactive(conversation_id, agent, body.task, active))
    return _tail(conversation_id, q)


async def _run_interactive(conversation_id: int, agent: dict, task: str,
                           active: str | None) -> None:
    """One interactive agent run, detached from the HTTP connection that asked
    for it. Every event goes to the conversation's bus channel; the original
    POST and any later re-attach just watch. Persistence happens here either
    way, and the run ends with a notice on NOTICE_CHAN so the GUI can tell the
    operator it finished if they moved on."""
    from . import runtime
    ptoken = runtime.active_project.set(active)
    cidtoken = runtime.conversation_id.set(conversation_id)
    # own fetch-ledger scope, like headless runs: without it web claims fall
    # back to the project slug and never expire — a URL read today would be
    # "already claimed" for every future interactive run in this project
    wtoken = runtime.web_session.set(f"run:{conversation_id}")
    chan = _chan(conversation_id)
    started = time.monotonic()
    db = None
    final_content, error = "", None
    try:
        db = await get_db()
        bus.publish(chan, {"type": "start", "conversation_id": conversation_id,
                           "agent": agent["name"]})
        system_prompt = await _agent_system_prompt(db, agent, active=active)
        tools = _agent_tools(agent, await _project_autonomy(db, active))
        mdl, burl = _agent_overrides(agent)
        history = [{"role": "user", "content": task}]
        async for event in run_agent_turn(conversation_id, system_prompt, history,
                                          tools=tools, model_name=mdl, base_url=burl,
                                          active_project=active,
                                          on_tool_call=db_tool_sink(db, conversation_id)):
            if event["type"] == "final":
                final_content = event["content"]
            else:
                bus.publish(chan, event)
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) "
            "VALUES (?, 'assistant', ?)", (conversation_id, final_content))
        await db.commit()
        bus.publish(chan, {"type": "final", "content": final_content})
    except asyncio.CancelledError:
        error = "run cancelled"
        bus.publish(chan, {"type": "error", "message": error})
        raise
    except Exception as e:  # noqa: BLE001 — surface to the GUI, don't 500 mid-stream
        error = str(e)
        bus.publish(chan, {"type": "error", "message": error})
    finally:
        # the notice fires however the run ended — an agent that died after the
        # operator walked away is exactly the case worth telling them about
        try:
            bus.publish(NOTICE_CHAN, {
                "type": "agent_run_done", "conversation_id": conversation_id,
                "agent": agent.get("name") or agent.get("slug"),
                "slug": agent.get("slug"), "project": active,
                "ok": error is None, "error": error,
                "took": _human_secs(time.monotonic() - started),
                "summary": " ".join((final_content or "").split())[:180]})
        except Exception:                        # noqa: BLE001 — never break the run
            pass
        bus.publish(chan, bus.JOB_END)
        _active_runs.pop(conversation_id, None)
        if db is not None:
            await db.close()
        runtime.web_session.reset(wtoken)
        runtime.conversation_id.reset(cidtoken)
        runtime.active_project.reset(ptoken)


def _tail(conversation_id: int, q) -> StreamingResponse:
    """SSE-forward one run's bus channel. A client disconnect cancels only this
    tail — never the run."""
    async def event_stream():
        try:
            while True:
                ev = await q.get()
                if ev.get("type") == "job_end":
                    break
                yield sse(ev)
                if ev.get("type") in ("final", "error"):
                    break
        finally:
            bus.unsubscribe(_chan(conversation_id), q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/runs/{conversation_id}/stream")
async def resume_run_stream(conversation_id: int):
    """Re-attach to an in-flight run (came back to the board, reloaded the
    page). Tokens streamed before attaching are gone, but the final event
    carries the whole reply."""
    q = bus.subscribe(_chan(conversation_id))
    if conversation_id not in _active_runs:
        bus.unsubscribe(_chan(conversation_id), q)

        async def idle():
            yield sse({"type": "idle", "conversation_id": conversation_id})

        return StreamingResponse(idle(), media_type="text/event-stream")
    return _tail(conversation_id, q)


@router.get("/notices/stream")
async def notice_stream():
    """Completion notices for operator-started agent runs (see NOTICE_CHAN)."""
    q = bus.subscribe(NOTICE_CHAN)

    async def gen():
        try:
            yield sse({"type": "stream_open"})
            while True:
                try:
                    yield sse(await asyncio.wait_for(q.get(), timeout=25))
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(NOTICE_CHAN, q)

    return StreamingResponse(gen(), media_type="text/event-stream")
