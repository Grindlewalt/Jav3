import asyncio
import json
import shutil
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import autonomy, bus, compaction, runtime
from .agent import budget
from .agent.model import confirm_peak, in_peak_window, model, peak_confirmed
from .agent.loop import db_tool_sink, run_turn
from .agent.tools.registry import load_registry, openai_tool_specs
from .auth import require_user
from .config import settings
from .db import get_db, open_conversation
from .memory import assemble_system_prompt, get_active_project

router = APIRouter(prefix="/api", tags=["chat"], dependencies=[Depends(require_user)])


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None
    confirm_peak: bool = False
    ephemeral: bool = False   # incognito: persist nothing, memory writes go to a temp dir


def sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


async def _name_conversation(conversation_id: int, user_msg: str, reply: str) -> None:
    """Ask the model for a short title. Fails silently — the truncated
    first-message title stays if the call errors (no balance, offline...)."""
    try:
        final = None
        async for ev in model.complete([
            {"role": "system",
             "content": "Name this chat in 3-6 words. Reply with only the title."},
            {"role": "user",
             "content": f"User: {user_msg[:400]}\n\nAssistant: {reply[:400]}"},
        ]):
            if ev["type"] == "message":
                final = ev
        title = (final["content"] or "").strip().strip('"').strip()[:60]
        if not title:
            return
        db = await get_db()
        try:
            await db.execute("UPDATE conversations SET summary = ? WHERE id = ?",
                             (title, conversation_id))
            await db.commit()
        finally:
            await db.close()
    except Exception:
        pass


class AssignProject(BaseModel):
    project: str | None  # slug, or null to detach


@router.get("/conversations")
async def list_conversations(project: str | None = None):
    db = await get_db()
    try:
        # only real chats in the sidebar — head/leader/subagent job nodes live
        # on the Runs page, not here
        q = ("SELECT c.*, p.slug AS project_slug, p.name AS project_name "
             "FROM conversations c LEFT JOIN projects p ON p.id = c.project_id "
             "WHERE (c.kind = 'chat' OR c.kind IS NULL) ")
        params: tuple = ()
        if project:
            q += "AND p.slug = ? "
            params = (project,)
        q += "ORDER BY c.started_at DESC"
        async with db.execute(q, params) as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()
    # `running` lets a remounted panel find and re-attach to an in-flight turn
    return {"conversations": [{**dict(r), "running": r["id"] in _active_turns}
                              for r in rows]}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such conversation")
        await db.execute("DELETE FROM tool_calls WHERE conversation_id = ?", (conversation_id,))
        await db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        await db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        await db.commit()
    finally:
        await db.close()
    return {"ok": True}


@router.patch("/conversations/{conversation_id}")
async def assign_conversation(conversation_id: int, body: AssignProject):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such conversation")
        project_id = None
        if body.project:
            async with db.execute(
                "SELECT id FROM projects WHERE slug = ? AND deleted_at IS NULL",
                (body.project,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="no such project")
            project_id = row["id"]
        await db.execute(
            "UPDATE conversations SET project_id = ? WHERE id = ?",
            (project_id, conversation_id),
        )
        await db.commit()
    finally:
        await db.close()
    return {"ok": True, "project": body.project}


@router.get("/conversations/{conversation_id}/messages")
async def get_messages(conversation_id: int):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY id", (conversation_id,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT tool, args, result, created_at FROM tool_calls "
            "WHERE conversation_id = ? ORDER BY id", (conversation_id,)
        ) as cur:
            calls = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    # attach each turn's tool calls to the assistant message that closed the
    # turn (calls always precede it), so the activity dropdown survives a
    # reload instead of existing only in the live stream
    def _act(c: dict) -> dict:
        try:
            args = json.loads(c["args"] or "{}")
        except json.JSONDecodeError:
            args = {}
        result = c["result"] or ""
        return {"name": c["tool"], "args": args, "result": result,
                "ok": not result.startswith(("error:", "duplicate call:")),
                "done": True}

    ci = 0
    for m in rows:
        if m["role"] != "assistant":
            continue
        acts = []
        while ci < len(calls) and calls[ci]["created_at"] <= m["created_at"]:
            acts.append(_act(calls[ci]))
            ci += 1
        if acts:
            m["activity"] = acts
    # `running` lets the GUI re-attach to an in-flight turn after a reload;
    # calls past the last assistant message belong to that in-flight turn —
    # without them a reopened chat shows the current turn as a bare spinner
    # even though half its work is already persisted
    running = conversation_id in _active_turns
    pending = [_act(c) for c in calls[ci:]] if running else []
    return {"messages": rows, "running": running, "pending_activity": pending}


# In-flight turns, keyed by conversation. The dict entry is both the "is a
# turn running" flag and the strong reference that keeps the task alive after
# the HTTP connection that started it goes away.
_active_turns: dict[int, asyncio.Task] = {}


def _chan(conversation_id: int) -> str:
    return f"chat:{conversation_id}"


# tools that may fall back to a chat's hidden artifact store (via
# toolctx.require_project): the file tools, plus the plan/orchestrate pair so
# a big ask in plain chat still gets a todo plan and an agent team instead of
# a hand-rolled turn. run/git/search tools stay strictly project-only.
ARTIFACT_TOOLS = frozenset({"write_file", "edit_file", "read_file", "list_files",
                            "todo_update", "deploy_agents"})

# a turn that used any of these did real project work — journal-worthy
_JOURNAL_WORTHY = frozenset({"write_file", "edit_file", "git_commit_request"})


async def _project_autonomy(db, slug: str) -> str | None:
    """The project's autonomy level (None == full/unrestricted)."""
    async with db.execute("SELECT autonomy FROM projects WHERE slug = ?",
                          (slug,)) as cur:
        row = await cur.fetchone()
    return row["autonomy"] if row else None


async def _auto_journal(db, conversation_id: int, user_msg: str, final: str,
                        before_id: int) -> None:
    """F5 interim: if this turn mutated the active project and never called
    journal_update itself, write one auto line so project.md stays current.
    Best-effort — a failure here never touches the turn. (The fuller design
    waits on the claude-code-expert consult.)"""
    if not settings.auto_journal:
        return
    if not await get_active_project(db):
        return
    async with db.execute(
        "SELECT DISTINCT tool FROM tool_calls WHERE conversation_id = ? AND id > ?",
        (conversation_id, before_id)) as cur:
        tools = {r["tool"] for r in await cur.fetchall()}
    if "journal_update" in tools or not tools & _JOURNAL_WORTHY:
        return
    from .agent.tools import registry
    from .summarize import complete_text
    line = " ".join((await complete_text(
        "Write ONE tight project-journal line (max 20 words) describing what "
        "was just done. Past tense, no preamble, no quotes.",
        f"Request: {user_msg[:400]}\n\nOutcome: {final[:800]}")).split())
    if line:
        await registry.dispatch("journal_update", {"entry": f"(auto) {line[:200]}"})


async def _run_chat_turn(conversation_id: int, ephemeral: bool,
                         user_msg: str = "") -> None:
    """One whole chat turn, detached from any HTTP connection: clicking off
    the tab no longer kills the work. Every event is published to the
    conversation's bus channel; any number of SSE tails (the original POST,
    a reconnect) just watch. Persistence happens here regardless."""
    token = runtime.ephemeral.set(ephemeral)
    # one token budget for the whole turn, shared by any tools/agents it spawns
    the_budget = budget.Budget(
        settings.max_op_input_tokens, settings.max_op_output_tokens)
    op_id = f"chat:{conversation_id}"
    budget.register(op_id, the_budget)
    optoken = budget.active_op_id.set(op_id)
    chan = _chan(conversation_id)
    ctoken = runtime.event_chan.set(chan)
    # fresh fetch-ledger scope per turn: parallel reads inside the turn (and
    # any team it deploys) dedup, while tomorrow's turn can re-read the page
    wtoken = runtime.web_session.set(f"turn:{conversation_id}:{uuid.uuid4().hex[:8]}")
    atoken = None
    db = await get_db()
    try:
        bus.publish(chan, {"type": "start", "conversation_id": conversation_id})
        system_prompt = await assemble_system_prompt(db)
        # tool subsetting: with no project loaded, project-scoped run/git/
        # search tools can only error — withhold them. The FILE tools stay:
        # they fall back to the chat's hidden artifact store (persistent
        # chats only; incognito leaves no trace). The set is stable within a
        # project state, so the provider's prefix cache survives.
        entries = load_registry()
        active = await get_active_project(db)
        if not active:
            if ephemeral:
                entries = [e for e in entries if not e.get("requires_project")]
            else:
                atoken = runtime.artifact_slug.set(f"chat-{conversation_id}")
                entries = [e for e in entries
                           if not e.get("requires_project")
                           or e["name"] in ARTIFACT_TOOLS]
        else:
            # per-project autonomy dial: withhold tools above the project's level
            entries = autonomy.filter_entries(entries, await _project_autonomy(db, active))
        tools = openai_tool_specs(entries)
        # tier-2 compaction: summary (if any) + verbatim tail, compacting
        # first when the effective context window demands it
        history = await compaction.assemble(db, conversation_id, system_prompt)

        async with db.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM tool_calls "
            "WHERE conversation_id = ?", (conversation_id,)) as cur:
            tools_before = (await cur.fetchone())["m"]

        final_content = ""
        async for event in run_turn(conversation_id, system_prompt, history,
                                    tools=tools,
                                    on_tool_call=db_tool_sink(db, conversation_id)):
            if event["type"] == "final":
                final_content = event["content"]
            else:
                bus.publish(chan, event)

        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'assistant', ?)",
            (conversation_id, final_content),
        )
        await db.commit()
        if not ephemeral:
            async with db.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ) as cur:
                count = (await cur.fetchone())["c"]
            if count == 2:  # first exchange done — try to give it a real name
                asyncio.create_task(
                    _name_conversation(conversation_id, user_msg, final_content))
            try:
                await _auto_journal(db, conversation_id, user_msg,
                                    final_content, tools_before)
            except Exception:  # noqa: BLE001 — journaling never breaks a turn
                pass
        bus.publish(chan, {"type": "final", "content": final_content,
                           "conversation_id": conversation_id})
    except Exception as exc:  # surfaced to any tail rather than lost
        bus.publish(chan, {"type": "error", "message": str(exc)})
    finally:
        if ephemeral:
            # incognito: leave zero trace — drop the convo and any temp notes
            for tbl in ("tool_calls", "messages", "conversations"):
                col = "id" if tbl == "conversations" else "conversation_id"
                await db.execute(f"DELETE FROM {tbl} WHERE {col} = ?", (conversation_id,))
            await db.commit()
            shutil.rmtree(settings.memory_dir / ".ephemeral-notes", ignore_errors=True)
        else:
            try:
                await db.execute(
                    "INSERT INTO usage_log (conversation_id, input_tokens, "
                    "output_tokens, cache_hit, cache_miss) VALUES (?,?,?,?,?)",
                    (conversation_id, the_budget.input_tokens,
                     the_budget.output_tokens, the_budget.cache_hit,
                     the_budget.cache_miss))
                await db.commit()
            except Exception:
                pass
        if atoken is not None:
            runtime.artifact_slug.reset(atoken)
        runtime.web_session.reset(wtoken)
        runtime.event_chan.reset(ctoken)
        runtime.ephemeral.reset(token)
        budget.active_op_id.reset(optoken)
        budget.release(op_id)
        await db.close()
        # order matters for the reconnect race: drop the running flag, THEN
        # signal end — a subscriber that still sees the flag is guaranteed
        # the job_end is ahead of it in the queue (both happen in this tick)
        _active_turns.pop(conversation_id, None)
        bus.close_job(chan)


def _tail(conversation_id: int, q) -> "StreamingResponse":
    """SSE-forward a conversation's bus channel until the turn ends. Client
    disconnect cancels only this tail, never the turn."""
    chan = _chan(conversation_id)

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
            bus.unsubscribe(chan, q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/chat/{conversation_id}/stream")
async def resume_chat_stream(conversation_id: int):
    """Re-attach to an in-flight turn (page reload, coming back to the tab).
    Tokens streamed before attaching are gone, but the final event carries the
    complete reply, so the GUI ends up whole either way."""
    q = bus.subscribe(_chan(conversation_id))
    if conversation_id not in _active_turns:
        # subscribe-then-check closes the race with the turn's finally block
        bus.unsubscribe(_chan(conversation_id), q)

        async def idle():
            yield sse({"type": "idle"})
        return StreamingResponse(idle(), media_type="text/event-stream")
    return _tail(conversation_id, q)


@router.post("/chat")
async def chat(body: ChatRequest):
    db = await get_db()
    try:
        conversation_id = body.conversation_id
        if conversation_id is not None and conversation_id in _active_turns:
            raise HTTPException(status_code=409, detail="turn_in_progress")
        if conversation_id is None:
            # Peak-cost gate (spec §4) BEFORE the conversation exists: the old
            # order created the row first, so this 409 left an orphan,
            # blank-rendering conversation behind (and the retry opened a
            # fresh one — twin entries in the sidebar).
            if in_peak_window() and not body.confirm_peak:
                raise HTTPException(status_code=409,
                                    detail="peak_confirmation_required")
            active = await get_active_project(db)
            # provisional title: first bit of the opening message; an LLM
            # naming pass upgrades it after the first exchange (best effort)
            title = " ".join(body.message.split())[:48] or "(empty)"
            conversation_id = await open_conversation(db, project=active, title=title)
            if body.confirm_peak:
                confirm_peak(conversation_id)
        else:
            async with db.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ) as cur:
                if not await cur.fetchone():
                    raise HTTPException(status_code=404, detail="no such conversation")
            # Peak-cost gate for an existing conversation: confirmation is
            # keyed to its id, so it can (and must) be checked after lookup.
            if body.confirm_peak:
                confirm_peak(conversation_id)
            if in_peak_window() and not peak_confirmed(conversation_id):
                raise HTTPException(
                    status_code=409,
                    detail="peak_confirmation_required",
                    headers={"X-Conversation-Id": str(conversation_id)},
                )

        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
            (conversation_id, body.message),
        )
        await db.commit()
    finally:
        await db.close()

    # subscribe BEFORE spawning so this tail can't miss the first events, then
    # run the turn as a detached task: it outlives this HTTP connection
    q = bus.subscribe(_chan(conversation_id))
    _active_turns[conversation_id] = asyncio.create_task(
        _run_chat_turn(conversation_id, body.ephemeral, body.message))
    return _tail(conversation_id, q)
