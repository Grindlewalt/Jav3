"""Running an agent = a ReAct turn with the agent's own system prompt and a
tool set trimmed by its exclusions, streamed and persisted like a chat so the
run is findable afterward. This is the concrete implementation behind the
Agents tab's definitions; the operator kicks one off from the project board.

The agent runs in the ACTIVE PROJECT: it gets the project's assembled context
(minus any context items the agent excludes) and the same staged-write tools,
so its file changes land in the approval queue exactly like Jav3's own.
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
# one marker for "the operator stopped this", shared with the chat path so a
# stopped run and a stopped turn read identically in a transcript
from .chat import INTERRUPTED_MARKER
from .config import settings
from .db import get_db, launcher, open_conversation
from .memory import assemble_system_prompt, get_active_project

router = APIRouter(prefix="/api/agents", tags=["agents"],
                   dependencies=[Depends(require_user)])

# Its own prefix, not /api/agents/messages: agents_api's `GET /api/agents/{slug}`
# is registered first and would swallow a one-segment sibling as an agent named
# "messages". A message is also not an agent's sub-resource — it is addressed to
# a running turn, which may be a plain chat with no agent at all.
messages_router = APIRouter(prefix="/api/messages", tags=["agents"],
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


def agent_exclusions(agent: dict) -> set[str]:
    """Registry entry names this definition removes.

    Skills compile into the SAME registry as tools (registry.py:_sources), so
    there is one namespace to exclude from — `skills_exclude` was declared and
    stored for a long time while biting nothing. Every path that trims an
    agent's tools (runs, spawned children, chat threads) goes through here."""
    return (set(agent.get("tools_exclude") or [])
            | set(agent.get("skills_exclude") or []))


def memory_slug(agent: dict) -> str | None:
    """The slug whose private notes dir this agent's memory tools use, or None
    for the shared notes. Only a real definition (it has a slug) can own one —
    a temp agent's notes are the whole point of it surviving, so they stay
    shared where the operator and Jav3 will see them."""
    return agent.get("slug") if agent.get("own_memory") and agent.get("slug") else None


def _agent_tools(agent: dict, autonomy_level: str | None = None) -> list[dict]:
    from . import autonomy, runtime
    own_exclude = agent_exclusions(agent)
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


async def bound_project(db, agent: dict) -> str | None:
    """The definition's own `project`, checked live — or None if it has none.
    A bound project deleted since the definition was saved is a 404, not a
    silent fallback: running a project's agent somewhere else is the wrong
    work in the wrong tree."""
    slug = (agent.get("project") or "").strip()
    if not slug:
        return None
    async with db.execute(
        "SELECT 1 FROM projects WHERE slug = ? AND deleted_at IS NULL",
        (slug,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(
                status_code=404,
                detail=f"agent '{agent.get('name')}' is bound to project "
                       f"'{slug}', which no longer exists — edit the agent")
    return slug


async def resolve_run_project(db, agent: dict, requested=_USE_DB) -> str | None:
    """Where an agent run happens: the request's `project` > the definition's
    `project` > the caller's pin (a project-bound chat that spawned us) > the
    GUI's global active project.

    `requested` is _USE_DB or None when the caller named nothing (None is how a
    schedule without a project_slug says it); a slug is validated here."""
    if requested is not _USE_DB and requested is not None:
        await _validate_project(db, requested)
        return requested
    bound = await bound_project(db, agent)
    if bound:
        return bound
    # a schedule's "no project" stays no project; only an unset caller inherits
    return None if requested is None else await _inherited_or_global(db)


async def _agent_system_prompt(db, agent: dict, active=_USE_DB,
                               extra_exclude: set[str] | None = None) -> str:
    """The agent's prompt, then the shared project context minus excluded
    sections. The agent's context_exclude tokens (soul.md, user.md, env.md,
    all-projects.md, active-project, ...) are assemble_system_prompt's block
    labels, so exclusion happens at assembly instead of post-hoc splitting.

    `extra_exclude` is the CALLER's own trimming, unioned with the agent's: the
    chat path uses it for the voice local tier's slim sandwich. This is the one
    place the "agent prompt + trimmed context" shape is built, so an agent chat
    thread and a one-shot run assemble their prompt identically."""
    exclude = set(agent.get("context_exclude") or []) | set(extra_exclude or ())
    base = (await assemble_system_prompt(db, exclude=exclude) if active is _USE_DB
            else await assemble_system_prompt(db, active=active, exclude=exclude))
    return f"{agent['prompt']}\n\n---\n\n{base}"


async def _open_run(db, agent: dict, task: str, active=_USE_DB, *,
                    job_id: str | None = None,
                    title: str | None = None) -> tuple[int, str | None]:
    """Create the conversation for an agent run and record the task. Returns
    (conversation_id, resolved project slug) — the caller needs the resolved
    slug (not the _USE_DB sentinel) for the autonomy lookup.

    `job_id` files the run under a job so the run tree (runs_api) shows it as
    that job's node; `title` overrides the default `[name] task…` summary —
    the plan runner titles items `[item i3] …` so the peer roster names them."""
    active = await resolve_run_project(db, agent, active)
    title = title or f"[{agent['name']}] " + " ".join(task.split())[:40]
    # parent: the turn that dispatched spawn_agent/spawn_temp_agent (the broker
    # restores runtime.conversation_id for a guest turn), so the run tree stays
    # connected exactly where Jav3 delegates. None for a schedule, which
    # really has no parent conversation.
    parent, _ = await launcher(db)
    conversation_id = await open_conversation(
        db, project=active, title=title, kind="agent", commit=False,
        parent=parent, job_id=job_id,
        # WHO this run is. A temp agent has no roster entry and gets None.
        agent=agent.get("slug"))
    await db.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
        (conversation_id, task))
    await db.commit()
    return conversation_id, active


async def run_agent_headless(slug: str, task: str, active=_USE_DB, **hooks) -> dict:
    """Run a defined agent to completion, no streaming — for scheduled runs and
    the spawn_agent tool. `hooks` are _run_headless's keyword hooks."""
    agent = _read(slug)  # 404s if missing
    return await _run_headless(agent, task, active, **hooks)


# blocks a lean temp agent drops: Jav3's identity, standing memory, the user
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
    a full copy of Jav3's context only when the task truly needs it."""
    return {
        "name": (label or "").strip()[:40] or "temp agent",
        "prompt": prompt.strip() + "\n\n" + TEMP_REPORT_BACK,
        "description": "", "model": "", "base_url": "", "own_memory": False,
        "context_exclude": [] if duplicate else list(TEMP_LEAN_EXCLUDE),
        "tools_exclude": [], "skills_exclude": [], "max_iterations": 0,
        "project": "",
    }


async def run_temp_agent_headless(prompt: str, task: str, *,
                                  duplicate: bool = False, label: str = "",
                                  active=_USE_DB, **hooks) -> dict:
    """A disposable agent: no AGENT.md, no roster entry — a role prompt layered
    on Jav3's own context (full when duplicate, lean otherwise), run once and
    gone. What survives is the run's conversation row (Jobs view) and any
    memory note the agent writes."""
    return await _run_headless(_temp_agent_def(prompt, duplicate, label),
                               task, active, **hooks)


def _internal_specs(names: tuple[str, ...]) -> list[dict]:
    """Specs for registry entries that are `enabled: false` — tools meant for
    one kind of turn only (plan_report for a plan item), granted explicitly by
    the caller that runs that kind of turn rather than to every turn."""
    wanted = set(names)
    return openai_tool_specs([{**e, "enabled": True} for e in load_registry()
                              if e["name"] in wanted])


async def _run_headless(agent: dict, task: str, active=_USE_DB, *,
                        job_id: str | None = None, title: str | None = None,
                        on_open=None, on_event=None,
                        extra_tools: tuple[str, ...] = ()) -> dict:
    """Shared engine for named and temp headless runs. Peak is auto-confirmed:
    the caller (a schedule or Jav3 itself) already intended this, there's no
    human to prompt. `active` pins the project context without disturbing the
    operator's live session.

    Headless runs are subagents of something (a parent turn or a schedule), so
    they get the tight subagent iteration cap unless the agent's definition
    grants more via max_iterations — the full 40-round chat cap is what let a
    subagent read dozens of pages and snowball its context.

    The keyword hooks exist for a supervisor that runs MANY of these and needs
    to watch them (plan.py): `on_open(cid)` is awaited as soon as the run's
    conversation exists (so it can be addressed and monitored before the first
    model call), `on_event(ev)` sees every loop event (activity for stall
    detection, tool events for the live tree), `job_id`/`title` file the run
    under a job, and `extra_tools` grants internal (`enabled: false`) tools."""
    from . import runtime
    db = await get_db()
    try:
        # take the RESOLVED slug back: _project_autonomy below binds `active`
        # as an SQL parameter, and the raw _USE_DB object() crashes aiosqlite
        conversation_id, active = await _open_run(db, agent, task, active=active,
                                                  job_id=job_id, title=title)
        if on_open is not None:
            await on_open(conversation_id)
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
        if extra_tools:
            tools = tools + _internal_specs(extra_tools)
        mdl, burl = _agent_overrides(agent)
        cap = agent.get("max_iterations") or settings.subagent_max_iterations
        history = [{"role": "user", "content": task}]
        final_content = ""
        try:
            async for event in run_agent_turn(conversation_id, system_prompt, history,
                                              tools=tools, model_name=mdl,
                                              base_url=burl, max_iterations=cap,
                                              active_project=active,
                                              memory_slug=memory_slug(agent),
                                              on_tool_call=db_tool_sink(db, conversation_id)):
                if on_event is not None:
                    on_event(event)
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
        # request > definition > the GUI's global (no caller pin over HTTP)
        active = await resolve_run_project(db, agent, body.project or _USE_DB)
        title = f"[{agent['name']}] " + " ".join(body.task.split())[:40]
        conversation_id = await open_conversation(
            db, project=active, title=title, kind="agent", agent=slug)

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
                           "agent": agent["name"], "agent_slug": agent.get("slug")})
        system_prompt = await _agent_system_prompt(db, agent, active=active)
        tools = _agent_tools(agent, await _project_autonomy(db, active))
        mdl, burl = _agent_overrides(agent)
        # max_iterations used to be honoured headless and ignored here, so one
        # definition ran two caps depending on who started it. An interactive
        # run is operator-started and watched, so its DEFAULT is the full chat
        # cap (None), not the subagent fence for unattended nesting.
        cap = agent.get("max_iterations") or None
        history = [{"role": "user", "content": task}]
        async for event in run_agent_turn(conversation_id, system_prompt, history,
                                          tools=tools, model_name=mdl, base_url=burl,
                                          max_iterations=cap, active_project=active,
                                          memory_slug=memory_slug(agent),
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
        # the operator hit stop. Same contract as chat.py's stop: leave the
        # interruption in the transcript so a reopened run doesn't look like it
        # silently produced nothing, publish a final so every attached tail
        # settles, then re-raise so the task ends properly cancelled.
        error = "run cancelled"
        final_content = INTERRUPTED_MARKER
        if db is not None:
            try:
                await db.execute(
                    "INSERT INTO messages (conversation_id, role, content) "
                    "VALUES (?, 'assistant', ?)",
                    (conversation_id, INTERRUPTED_MARKER))
                await db.commit()
            except Exception:  # noqa: BLE001 — the marker is best-effort
                pass
        bus.publish(chan, {"type": "final", "content": INTERRUPTED_MARKER})
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
        # drop the running flag, THEN signal end (chat.py's order):
        # resume_run_stream subscribes and then checks the flag, so with the end
        # published first a re-attach landing between the two waited forever
        # on a channel whose terminal event had already gone out to nobody
        _active_runs.pop(conversation_id, None)
        bus.publish(chan, bus.JOB_END)
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


@router.post("/runs/{conversation_id}/stop")
async def stop_run(conversation_id: int):
    """Cancel an in-flight agent run (mirror of POST /api/chat/{cid}/stop).
    The run's CancelledError handler records the interruption, publishes a
    final event and fires the completion notice, so every attached tail (and
    the transcript) settles on its own."""
    task = _active_runs.get(conversation_id)
    if task is None or task.done():
        return {"stopped": False}
    task.cancel()
    return {"stopped": True}


# Everything an agent produced: conversations that run AS the slug, plus every
# node descended from one (spawned children, temp agents, funnel/research jobs
# it launched). UNION, not UNION ALL, so a malformed parent cycle terminates.
_OUTPUTS_SQL = """
WITH RECURSIVE tree(id) AS (
    SELECT id FROM conversations WHERE agent_slug = ?
    UNION
    SELECT c.id FROM conversations c JOIN tree t ON c.parent_conversation_id = t.id
)
SELECT c.id, c.kind, c.summary AS title, c.agent_slug,
       c.parent_conversation_id AS parent_id, c.job_id, c.rollup, c.started_at,
       p.slug AS project,
       (SELECT m.content FROM messages m WHERE m.conversation_id = c.id
         ORDER BY m.id DESC LIMIT 1) AS last_message,
       (SELECT MAX(m.created_at) FROM messages m
         WHERE m.conversation_id = c.id) AS last_at
FROM conversations c JOIN tree t ON t.id = c.id
LEFT JOIN projects p ON p.id = c.project_id
ORDER BY c.started_at DESC, c.id DESC
LIMIT ?
"""


def _runs_files(project: str | None, job_id: str | None) -> list[str]:
    """The job's rollup files (orchestrator/research write runs/<job>/*.md into
    the project), as project-relative paths; empty when there are none."""
    if not (project and job_id):
        return []
    d = settings.projects_dir / project / "runs" / job_id
    if not d.is_dir():
        return []
    return sorted(f"runs/{job_id}/{p.name}" for p in d.iterdir() if p.is_file())


@router.get("/{slug}/outputs")
async def agent_outputs(slug: str, limit: int = 50):
    """The Agent Outputs view's data: newest first, any kind. Deliberately not
    a 404 for an unknown slug — a deleted agent's past work is still its past
    work. The live tail is the existing streams (agent runs' `start` event and
    job `job_start`/`node_spawned` events carry `agent_slug`)."""
    from .chat import _active_turns
    db = await get_db()
    try:
        async with db.execute(_OUTPUTS_SQL, (slug, max(1, min(limit, 200)))) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    for r in rows:
        last = " ".join((r.pop("last_message") or "").split())
        r["snippet"] = last[:240]
        r["running"] = r["id"] in _active_runs or r["id"] in _active_turns
        r["runs_files"] = _runs_files(r["project"], r["job_id"])
    return {"slug": slug, "outputs": rows}


@router.get("/{slug}/memory")
async def agent_memory_notes(slug: str):
    """An own_memory agent's private notes (agents/<slug>/memory/), so the
    operator can see what it keeps — a silo nobody can read would be worse than
    no silo. Empty for an agent that uses the shared notes."""
    from .memory import note_description, parse_note
    _read(slug)  # 404s if missing
    d = settings.agents_dir / slug / "memory"
    notes = []
    for p in sorted(d.glob("*.md")) if d.is_dir() else []:
        meta, body = parse_note(p.read_text())
        notes.append({"name": p.stem, "description": note_description(meta, body),
                      "body": body})
    return {"slug": slug, "notes": notes}
@messages_router.get("")
async def list_agent_messages(limit: int = 50):
    """Inter-agent messages, newest first, plus who is addressable right now.

    The operator's window onto a channel that is otherwise invisible: an
    undelivered row here is a message waiting for an agent that has not run
    since, which is the one failure mode worth being able to see. Read-only —
    the GUI for this is deliberately not built yet (a concurrent session owns
    the frontend), but the data a panel would need is all here."""
    from . import agentmsg
    db = await get_db()
    try:
        async with db.execute(
            "SELECT id, from_conversation_id, from_label, to_conversation_id, "
            "to_agent_slug, project_slug, body, created_at, delivered_at, "
            "delivered_to FROM agent_messages ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 200)),)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        peers = await agentmsg.live_peers(db)
    finally:
        await db.close()
    return {"messages": rows, "running": peers,
            "undelivered": sum(1 for r in rows if r["delivered_at"] is None)}


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
