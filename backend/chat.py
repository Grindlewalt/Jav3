import asyncio
import json
import shutil
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import agentmsg, autonomy, bus, compaction, gui, providers, runtime
from .agent import budget
from .agent.model import confirm_peak, in_peak_window, model, peak_confirmed
from .agent.loop import db_tool_sink
from .agent.tools.registry import load_registry, openai_tool_specs, read_only_names
from .auth import require_actor
from .config import settings
from .db import get_db, open_conversation
from .memory import (assemble_system_prompt, estimate_tokens,
                     get_active_project, standing_rules_tail)
# module level, not function level: it is the turn's single loop entry now, and
# the offline tests substitute it here to run a turn without a model
from .vm.broker import TurnEnvelope
from .vm.guest_turn import guest_turn

# require_actor: the operator's cookie OR an enrolled device's Bearer token, so
# a paired CLI can drive chat. Sensitive control-plane routers stay require_user.
router = APIRouter(prefix="/api", tags=["chat"], dependencies=[Depends(require_actor)])


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None
    confirm_peak: bool = False
    # "temporary chat" in the GUI: persist nothing, memory writes go to a temp dir
    ephemeral: bool = False
    # pin a NEW conversation to this project (workspace chat panels pass their
    # slug); ignored for existing conversations — reassign via PATCH instead.
    project: str | None = None
    # same tri-state as AssignProject.mode. Omitted it keeps the old shape: a
    # slug pins, no slug follows the globally-loaded project.
    project_mode: Literal["follow", "none", "pin"] | None = None
    # run this NEW conversation as an agent (agents/<slug>/AGENT.md) instead of
    # as central Jav3. Like `project`, it binds at creation and is ignored for
    # an existing conversation: a thread's identity is what its transcript is
    # attributable to, so it must not shift mid-conversation. With no
    # project/project_mode given, the definition's own `project` pins the thread.
    agent: str | None = None
    # which browser tab is asking. The SPA sends the id it registered on
    # /api/gui/stream, so anything this turn plays comes out of the machine the
    # operator is sitting at instead of every open tab at once.
    tab: str | None = None
    # `provider/model` (a bare id = the default provider) from the enabled
    # list. Pins THIS conversation to it — new or existing, since switching
    # model mid-thread is ordinary; omitted keeps the thread's pin, and a
    # thread with none follows the default.
    model: str | None = None
    # "orchestrate" opens this NEW conversation as an orchestrator: it breaks
    # the operator's dump into a checklist run by a team of agents in
    # `project` (required), monitors and messages them, and reports. Binds at
    # creation like `project`/`agent`; ignored for an existing conversation,
    # which keeps whatever mode it was opened with.
    mode: Literal["orchestrate"] | None = None


class OperatorMessage(BaseModel):
    text: str


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
    # `title` renames the chat. The LLM naming pass only runs after the first
    # exchange and never again, so a chat that drifted keeps a title about its
    # opening message until somebody can change it by hand.
    title: str | None = None
    project: str | None = None   # slug to pin this chat to
    # "follow": inherit whatever project is loaded globally (the historic
    # meaning of a null project). "none": pinned to no project — file work
    # goes to the chat's artifact store instead. "pin": use `project`.
    # Omitted, it reads the old shape: a slug pins, a null follows.
    mode: Literal["follow", "none", "pin"] | None = None
    # sidebar organisation. Each is applied only when present in the body, so
    # a star toggle can never fall through to the project path; an explicit
    # null folder_id unfiles the chat.
    starred: bool | None = None
    folder_id: int | None = None


@router.get("/conversations")
async def list_conversations(project: str | None = None, folder: str | None = None):
    """`folder` narrows the list: a folder id, `none` (unfiled) or `starred`."""
    db = await get_db()
    try:
        # only real chats in the sidebar — head/leader/subagent job nodes live
        # on the Runs page, not here
        q = ("SELECT c.*, p.slug AS project_slug, p.name AS project_name "
             "FROM conversations c LEFT JOIN projects p ON p.id = c.project_id "
             "WHERE (c.kind = 'chat' OR c.kind IS NULL) ")
        params: list = []
        if project:
            q += "AND p.slug = ? "
            params.append(project)
        if folder == "none":
            q += "AND c.folder_id IS NULL "
        elif folder == "starred":
            q += "AND c.starred = 1 "
        elif folder is not None:
            if not folder.isdigit():
                raise HTTPException(status_code=400,
                                    detail="folder must be an id, 'none' or 'starred'")
            q += "AND c.folder_id = ? "
            params.append(int(folder))
        q += "ORDER BY c.started_at DESC, c.id DESC"
        async with db.execute(q, params) as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()
    # `running` lets a remounted panel find and re-attach to an in-flight turn
    return {"conversations": [{**dict(r), "starred": bool(r["starred"]),
                               "running": r["id"] in _active_turns}
                              for r in rows]}


async def _drop_references(db, conversation_id: int) -> None:
    """Clear every foreign key pointing AT this conversation, so deleting it
    doesn't hit `FOREIGN KEY constraint failed` (get_db sets foreign_keys=ON).

    Spawned agents and the funnel/research jobs a chat launches record it as
    their parent, so a chat that delegated has child rows. The children keep
    their own transcripts and rollups and simply become roots: the run
    happened, the conversation that asked for it is gone."""
    await db.execute(
        "UPDATE conversations SET parent_conversation_id = NULL "
        "WHERE parent_conversation_id = ?", (conversation_id,))
    # A message this conversation SENT is addressed to somebody else and may
    # still be unclaimed; the sender going away is no reason to destroy it. Only
    # the reply address goes (from_label is denormalised onto the row so it
    # still says who sent it). Deleting these was how an incognito turn's
    # messages vanished after the tool had promised delivery — that promise is
    # now refused up front (agentmsg.send_tool), and this clause is the second
    # half: an ordinary chat being deleted must not silently unsend its mail.
    await db.execute(
        "UPDATE agent_messages SET from_conversation_id = NULL "
        "WHERE from_conversation_id = ?", (conversation_id,))
    await db.execute(
        "UPDATE agent_messages SET delivered_to = NULL WHERE delivered_to = ?",
        (conversation_id,))
    # ...but a message addressed TO it can never be claimed by anyone now.
    await db.execute(
        "DELETE FROM agent_messages WHERE to_conversation_id = ?",
        (conversation_id,))


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such conversation")
        await _drop_references(db, conversation_id)
        await db.execute("DELETE FROM tool_calls WHERE conversation_id = ?", (conversation_id,))
        await db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        await db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        await db.commit()
    finally:
        await db.close()
    return {"ok": True}


@router.patch("/conversations/{conversation_id}")
async def assign_conversation(conversation_id: int, body: AssignProject):
    """Rename, star, file and/or re-bind a chat. Only the fields present in the
    body are touched: a star toggle or a rename must never reset the project
    binding, which is what an absent `project` used to mean."""
    sent = body.model_fields_set
    out: dict = {"ok": True}
    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such conversation")
        if body.title is not None:
            name = " ".join(body.title.split())[:120]
            if not name:
                raise HTTPException(status_code=400, detail="title cannot be blank")
            await db.execute("UPDATE conversations SET summary = ? WHERE id = ?",
                             (name, conversation_id))
            out["title"] = name
        if "starred" in sent and body.starred is not None:
            await db.execute("UPDATE conversations SET starred = ? WHERE id = ?",
                             (1 if body.starred else 0, conversation_id))
            out["starred"] = body.starred
        if "folder_id" in sent:
            if body.folder_id is not None:
                async with db.execute("SELECT 1 FROM chat_folders WHERE id = ?",
                                      (body.folder_id,)) as cur:
                    if not await cur.fetchone():
                        raise HTTPException(status_code=404, detail="no such folder")
            await db.execute("UPDATE conversations SET folder_id = ? WHERE id = ?",
                             (body.folder_id, conversation_id))
            out["folder_id"] = body.folder_id
        if "project" in sent or "mode" in sent:
            mode = body.mode or ("pin" if body.project else "follow")
            project_id = None
            if mode == "pin" and body.project:
                async with db.execute(
                    "SELECT id FROM projects WHERE slug = ? AND deleted_at IS NULL",
                    (body.project,),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="no such project")
                project_id = row["id"]
            await db.execute(
                "UPDATE conversations SET project_id = ?, project_locked = ? WHERE id = ?",
                (project_id, 0 if mode == "follow" else 1, conversation_id),
            )
            out.update(project=project_id and body.project, mode=mode)
        await db.commit()
    finally:
        await db.close()
    return out


# ---- chat folders --------------------------------------------------------
# Sidebar organisation only: a folder is a name and a place in the order. It
# has no bearing on a turn (no context, no project binding), and deleting one
# unfiles its chats rather than deleting them.

class FolderCreate(BaseModel):
    name: str


class FolderPatch(BaseModel):
    name: str | None = None
    position: int | None = None   # the index to move it to, 0 = first


def _folder_name(raw: str) -> str:
    name = " ".join(raw.split())[:60]
    if not name:
        raise HTTPException(status_code=400, detail="folder name cannot be blank")
    return name


async def _folder_rows(db) -> list[dict]:
    async with db.execute(
        "SELECT f.id, f.name, f.position, f.created_at, "
        "  (SELECT COUNT(*) FROM conversations c WHERE c.folder_id = f.id "
        "   AND (c.kind = 'chat' OR c.kind IS NULL)) AS count "
        "FROM chat_folders f ORDER BY f.position, f.id") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _name_taken(db, name: str, but: int | None = None) -> bool:
    async with db.execute(
        "SELECT 1 FROM chat_folders WHERE name = ? COLLATE NOCASE AND id IS NOT ?",
        (name, but)) as cur:
        return await cur.fetchone() is not None


@router.get("/chat/options")
async def chat_options():
    """What a new or existing chat can be pointed at: the enabled models, the
    projects and the agents, by name only. The terminal client's /model,
    /project and /agent pickers read this, since the full lists live on
    cookie-only routers a CLI token never reaches. Nothing here is more than
    POST /api/chat already accepts (and 404s on) by name."""
    from .agents_api import _list_dir
    db = await get_db()
    try:
        async with db.execute(
            "SELECT slug, name FROM projects WHERE deleted_at IS NULL "
            "AND is_hidden = 0 ORDER BY created_at DESC"
        ) as cur:
            projects = [dict(r) for r in await cur.fetchall()]
        active = await get_active_project(db)
    finally:
        await db.close()
    agents = [{k: a[k] for k in ("slug", "name", "description")}
              for a in _list_dir(settings.agents_dir)]
    return {**providers.models_payload(), "projects": projects,
            "active_project": active, "agents": agents}


_FILE_TOOLS = ("write_file", "edit_file")


@router.get("/conversations/{conversation_id}/info")
async def conversation_info(conversation_id: int):
    """One chat's running totals for the terminal client's sidebar: tokens and
    cost (priced like the Costs page), the last call's context against the
    model's window, and the files its turns wrote or edited."""
    from .logs_api import _cost_usd
    db = await get_db()
    try:
        async with db.execute(
            "SELECT c.summary, c.model, c.agent_slug, p.slug AS project_slug "
            "FROM conversations c LEFT JOIN projects p ON p.id = c.project_id "
            "WHERE c.id = ?", (conversation_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no such conversation")
        async with db.execute(
            "SELECT model, COUNT(*) n, COALESCE(SUM(input_tokens),0) i, "
            "COALESCE(SUM(output_tokens),0) o, COALESCE(SUM(cache_hit),0) ch, "
            "COALESCE(SUM(cache_miss),0) cm FROM model_calls "
            "WHERE conversation_id = ? GROUP BY model", (conversation_id,)) as cur:
            by_model = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT model, input_tokens FROM model_calls WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT 1", (conversation_id,)) as cur:
            last = await cur.fetchone()
        marks = ",".join("?" * len(_FILE_TOOLS))
        async with db.execute(
            f"SELECT tool, args FROM tool_calls WHERE conversation_id = ? "
            f"AND tool IN ({marks}) ORDER BY id", (conversation_id, *_FILE_TOOLS)) as cur:
            writes = await cur.fetchall()
    finally:
        await db.close()
    files: dict[str, int] = {}
    for w in writes:
        try:
            path = (json.loads(w["args"] or "{}") or {}).get("path")
        except (json.JSONDecodeError, AttributeError):
            path = None
        if isinstance(path, str) and path:
            files[path] = files.get(path, 0) + 1
    ctx = None
    if last is not None:
        pid, mid = providers.split_id(last["model"] or "")
        info = providers.model_info(pid, mid) if pid else None
        ctx = {"used": last["input_tokens"], "window": (info or {}).get("ctx")}
    return {"title": row["summary"], "model": row["model"],
            "agent": row["agent_slug"], "project": row["project_slug"],
            "calls": sum(m["n"] for m in by_model),
            "input_tokens": sum(m["i"] for m in by_model),
            "output_tokens": sum(m["o"] for m in by_model),
            "cost_usd": round(sum(_cost_usd(m["ch"], m["cm"], m["o"], m["model"])
                                  for m in by_model), 6),
            "context": ctx,
            "files": [{"path": p, "writes": n} for p, n in files.items()]}


@router.get("/chat/folders")
async def list_folders():
    db = await get_db()
    try:
        return {"folders": await _folder_rows(db)}
    finally:
        await db.close()


@router.post("/chat/folders")
async def create_folder(body: FolderCreate):
    name = _folder_name(body.name)
    db = await get_db()
    try:
        if await _name_taken(db, name):
            raise HTTPException(status_code=409, detail="a folder with that name exists")
        cur = await db.execute(
            "INSERT INTO chat_folders (name, position) "
            "VALUES (?, (SELECT COALESCE(MAX(position) + 1, 0) FROM chat_folders))",
            (name,))
        await db.commit()
        fid = cur.lastrowid
        folder = next(f for f in await _folder_rows(db) if f["id"] == fid)
    finally:
        await db.close()
    return {"ok": True, "folder": folder}


@router.patch("/chat/folders/{folder_id}")
async def update_folder(folder_id: int, body: FolderPatch):
    db = await get_db()
    try:
        rows = await _folder_rows(db)
        if not any(f["id"] == folder_id for f in rows):
            raise HTTPException(status_code=404, detail="no such folder")
        if body.name is not None:
            name = _folder_name(body.name)
            if await _name_taken(db, name, but=folder_id):
                raise HTTPException(status_code=409,
                                    detail="a folder with that name exists")
            await db.execute("UPDATE chat_folders SET name = ? WHERE id = ?",
                             (name, folder_id))
        if body.position is not None:
            # a move, not a raw write: take it out of the order, put it back at
            # the index, renumber densely — so two folders never share a slot
            order = [f["id"] for f in rows if f["id"] != folder_id]
            order.insert(max(0, min(body.position, len(order))), folder_id)
            for i, fid in enumerate(order):
                await db.execute("UPDATE chat_folders SET position = ? WHERE id = ?",
                                 (i, fid))
        await db.commit()
        folders = await _folder_rows(db)
    finally:
        await db.close()
    return {"ok": True, "folder": next(f for f in folders if f["id"] == folder_id),
            "folders": folders}


@router.delete("/chat/folders/{folder_id}")
async def delete_folder(folder_id: int):
    db = await get_db()
    try:
        async with db.execute("SELECT 1 FROM chat_folders WHERE id = ?",
                              (folder_id,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="no such folder")
        # ON DELETE SET NULL does this too; spelled out so the unfiling holds
        # even on a connection that forgot PRAGMA foreign_keys
        cur = await db.execute(
            "UPDATE conversations SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
        unfiled = cur.rowcount
        await db.execute("DELETE FROM chat_folders WHERE id = ?", (folder_id,))
        await db.commit()
    finally:
        await db.close()
    return {"ok": True, "unfiled": unfiled}


@router.get("/conversations/{conversation_id}/messages")
async def get_messages(conversation_id: int):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT id, role, content, model, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY id", (conversation_id,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT tool, args, result, created_at FROM tool_calls "
            "WHERE conversation_id = ? ORDER BY id", (conversation_id,)
        ) as cur:
            calls = [dict(r) for r in await cur.fetchall()]
        # the funnel/research jobs this conversation launched. The bus's `job`
        # announcement is live-only; the head's parent link is the durable
        # copy, so a reloaded chat can re-mount its JobTrees (created_at places
        # each one in the transcript; running = no rollup yet)
        async with db.execute(
            "SELECT id AS root_id, job_id, summary AS title, started_at AS created_at, "
            "rollup IS NULL AS running FROM conversations "
            "WHERE parent_conversation_id = ? AND kind = 'head' ORDER BY id",
            (conversation_id,)) as cur:
            jobs = [{**dict(r), "running": bool(r["running"])}
                    for r in await cur.fetchall()]
        async with db.execute(
            "SELECT agent_slug FROM conversations WHERE id = ?",
            (conversation_id,)) as cur:
            row = await cur.fetchone()
        agent_slug = row["agent_slug"] if row else None
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
    return {"messages": rows, "running": running, "pending_activity": pending,
            "agent_slug": agent_slug, "jobs": jobs}


# In-flight turns, keyed by conversation. The dict entry is both the "is a
# turn running" flag and the strong reference that keeps the task alive after
# the HTTP connection that started it goes away.
_active_turns: dict[int, asyncio.Task] = {}

# Voice barge-in: the orchestrator knows exactly how much of a reply was
# actually spoken, so it parks an annotated interruption note here before
# cancelling the turn task. The CancelledError handler writes the note in
# place of the bare marker. GUI stop sets nothing → old behavior.
_interrupt_notes: dict[int, str] = {}


def set_interrupt_note(conversation_id: int, note: str) -> None:
    _interrupt_notes[conversation_id] = note

# what an operator-stopped turn leaves behind, in the transcript and the
# final event — the GUI shows it verbatim
INTERRUPTED_MARKER = "[Request interrupted by operator]"


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


async def _link_tool_calls(db, conversation_id: int, before_id: int | None,
                           message_id: int | None) -> None:
    """Bind the tool_calls this turn produced to the assistant row it produced
    them for. That link is what lets compaction replay a past turn's tool work
    into the model-facing history instead of showing prose alone — see
    compaction.assemble(tool_trace=...). Best-effort: a turn is not worth
    failing over its own bookkeeping."""
    if before_id is None or message_id is None:
        return
    try:
        await db.execute(
            "UPDATE tool_calls SET message_id = ? WHERE conversation_id = ? "
            "AND message_id IS NULL AND id > ?",
            (message_id, conversation_id, before_id))
    except Exception:  # noqa: BLE001
        pass


async def _project_autonomy(db, slug: str) -> str | None:
    """The project's autonomy level (None == full/unrestricted)."""
    async with db.execute("SELECT autonomy FROM projects WHERE slug = ?",
                          (slug,)) as cur:
        row = await cur.fetchone()
    return row["autonomy"] if row else None


async def _auto_journal(db, conversation_id: int, user_msg: str, final: str,
                        before_id: int, active: str | None) -> None:
    """F5 interim: if this turn mutated its project and never called
    journal_update itself, write one auto line so project.md stays current.
    Best-effort — a failure here never touches the turn. (The fuller design
    waits on the claude-code-expert consult.)"""
    if not settings.auto_journal:
        return
    if not active:
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


def _agent_def(slug: str | None) -> dict | None:
    """The AGENT.md behind a conversation's identity, or None for Jav3.

    A missing or unparseable definition is an ERROR, not a silent fallback:
    running a thread the operator opened as `scout` under Jav3's own prompt
    would be the wrong agent answering under the right name. The exception
    surfaces on the turn's bus channel like any other turn failure."""
    if not slug:
        return None
    from .agents_api import _read as read_agent_def
    try:
        return read_agent_def(slug)
    except HTTPException as exc:
        raise RuntimeError(f"agent '{slug}': {exc.detail}") from None


async def _run_chat_turn(conversation_id: int, ephemeral: bool,
                         user_msg: str = "", tab: str | None = None,
                         voice: bool = False, model_name: str | None = None,
                         base_url: str | None = None,
                         context_exclude: tuple = (),
                         tools_only: tuple = ()) -> None:
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
    cidtoken = runtime.conversation_id.set(conversation_id)
    # the tab this was asked from, so anything the turn plays comes out of that
    # machine rather than every open Jav3 tab at once
    tabtoken = runtime.gui_tab.set(tab or None)
    if tab:
        gui.touch_tab(tab)
    atoken = None
    ptoken = None
    db = None
    tools_before = None      # set once the turn's tool_calls high-water mark is known
    late: list[str] = []     # operator messages the turn closed on without reading
    try:
        # inside the try: if the connect fails, the finally must still evict
        # _active_turns and close the bus channel or the conversation bricks
        # (every later POST 409s turn_in_progress) and its SSE tails hang
        db = await get_db()
        # the conversation's OWN project binding wins; pinning here (not the
        # global) is what lets chats in different projects run at the same time.
        # An unpinned chat follows the GUI's global active project — but a chat
        # pinned to nothing (project_locked with a NULL project) stays at no
        # project, and its file work lands in the artifact store below. Without
        # the lock there was no way to express that: a null binding was
        # indistinguishable from "not chosen yet" and inherited the last
        # project loaded.
        async with db.execute(
            "SELECT c.project_locked AS locked, c.agent_slug AS agent_slug, "
            "c.model AS model, c.mode AS mode, p.slug AS slug FROM conversations c "
            "LEFT JOIN projects p ON p.id = c.project_id AND p.deleted_at IS NULL "
            "WHERE c.id = ?", (conversation_id,)) as cur:
            row = await cur.fetchone()
        agent_slug = row["agent_slug"] if row else None
        # IDENTITY (read before `start` so the event can name the model the
        # turn runs on). A conversation bound to an agent slug runs AS that
        # agent: its AGENT.md prompt leads the sandwich and its exclusions bite.
        # Nothing else about the turn changes — multi-turn history, tier-2
        # compaction, the project pin, detach/re-attach and stop are all the
        # chat machinery, unmodified. A general agent is a chat with a name,
        # not a second runtime.
        agent_def = _agent_def(agent_slug)
        # model: the caller's routing (voice picks its tier per utterance and
        # must win) > the thread's own pin > the agent definition's > default
        if not voice and model_name is None and base_url is None:
            if row and row["model"]:
                model_name = row["model"]
            elif agent_def is not None:
                from .agents_run import _agent_overrides
                model_name, base_url = _agent_overrides(agent_def)
        model_name = providers.turn_model_id(model_name, base_url)
        bus.publish(chan, {"type": "start", "conversation_id": conversation_id,
                           "agent_slug": agent_slug, "model": model_name})
        if row and row["slug"]:
            active = row["slug"]
        elif row and row["locked"]:
            active = None
        else:
            active = await get_active_project(db)
        # tools deep in the loop (and spawn_agent children) resolve this pin
        # instead of the DB global — see toolctx.active_slug
        ptoken = runtime.active_project.set(active)
        # context_exclude: the voice local tier runs an 8B with a small ctx
        # window — it gets a slim sandwich (operator rules are never droppable)
        if agent_def is not None:
            from .agents_run import (_agent_system_prompt,
                                     memory_slug as _memory_slug)
            system_prompt = await _agent_system_prompt(
                db, agent_def, active=active,
                extra_exclude=set(context_exclude) or None)
        else:
            system_prompt = await assemble_system_prompt(
                db, active=active, exclude=set(context_exclude) or None)
        if voice:
            # spoken turns: narrate-before-acting + speakable-output rules.
            # Appended after everything (incl. the operator-rules tail) so it
            # rides the same end-of-prompt salience the rules rely on. A turn
            # routed to the local tier also gets the escalation protocol.
            from .tarmac import voice_library_prompt
            from .voice_text import (LOCAL_PROMPT, SMART_PROMPT,
                                     VOICE_CAPABILITIES, VOICE_PROMPT)
            system_prompt = f"{system_prompt}\n\n{VOICE_PROMPT}"
            if base_url:
                # the local tier also gets the capability map: its slim context
                # drops the behaviour bank, and a model that doesn't know the
                # system CAN do a thing refuses instead of escalating
                system_prompt = (f"{system_prompt}\n\n{VOICE_CAPABILITIES}"
                                 f"\n\n{LOCAL_PROMPT}")
                system_prompt += await voice_library_prompt()
            else:
                system_prompt = f"{system_prompt}\n\n{SMART_PROMPT}"
        # an orchestrator is a chat with a job description: the same turn,
        # plus the prompt that says how to run a team (after everything, the
        # voice block's reasoning) and, below, the tool to watch one
        orchestrating = bool(row and row["mode"] == "orchestrate")
        if orchestrating:
            from .plan import orchestrator_prompt
            system_prompt = f"{system_prompt}\n\n{orchestrator_prompt(active)}"
        # tool subsetting: with no project loaded, project-scoped run/git/
        # search tools can only error — withhold them. The FILE tools stay:
        # they fall back to the chat's hidden artifact store (persistent
        # chats only; incognito leaves no trace). The set is stable within a
        # project state, so the provider's prefix cache survives.
        entries = load_registry()
        if ephemeral:
            # a temporary chat cannot message another agent — delivery writes
            # the words into a permanent transcript. The handler refuses anyway
            # (agentmsg.send_tool, which is the authoritative check because a
            # brokered call from a child turn reaches it too), but a tool that
            # can only ever error should not be offered: it spends a schema on
            # every turn and invites the model to promise something it can't do.
            entries = [e for e in entries if e["name"] != "send_message"]
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
        if tools_only:
            # the voice local tier: a 4B gets a hand-picked conversational
            # toolset, not thirty schemas — everything else is escalation's job
            entries = [e for e in entries if e["name"] in tools_only]
        if agent_def is not None:
            # the definition's exclusions, applied LAST: an agent thread may
            # narrow what the project already allows, never widen it. NOT
            # agents_run._agent_tools — that also strips the delegation tools,
            # which is SUBAGENT policy; a thread the operator opened is
            # top-level and keeps whatever the project's autonomy dial grants.
            from .agents_run import agent_exclusions
            excluded = agent_exclusions(agent_def)
            entries = [e for e in entries if e["name"] not in excluded]
        if orchestrating and any(e["name"] == "orchestrate" for e in entries):
            # plan_status is `enabled: false` (an ordinary chat that launches a
            # plan is told not to wait on it); an orchestrator's whole job is
            # to. Granted only where orchestrate itself survived the project's
            # autonomy dial — watching a plan it may not start is pointless.
            entries = entries + [{**e, "enabled": True} for e in load_registry()
                                 if e["name"] == "plan_status"]
        # ...and a shortened Notes body. NOT zero: the first line of a body is
        # where the load-bearing operating instruction lives ("Do not call
        # music_search first"), and dropping it entirely broke tool use on the local
        # tier. 240 chars keeps that line and still sheds ~60% of the block.
        from .voice_text import LOCAL_NOTES_MAX
        tools = openai_tool_specs(entries,
                                  notes_max=LOCAL_NOTES_MAX if tools_only else None)
        # tier-2 compaction: summary (if any) + verbatim tail, compacting
        # first when the effective context window demands it. The voice local
        # tier also gets past turns' TOOL work replayed: a 4B reading a history
        # of prose-only replies concludes that announcing an action is the
        # action, and stops calling tools entirely (compaction._with_tool_trace).
        # It is sized against llama.cpp's slot rather than DeepSeek's 1M, too,
        # or a long session would never compact and would instead overflow —
        # which silently drops the front of the prompt, tool specs included.
        # The tool specs are measured, not guessed: they are the biggest and
        # most variable part of that budget, and they are right here.
        history = await compaction.assemble(
            db, conversation_id, system_prompt,
            tool_trace=settings.voice_local_tool_trace_chars if tools_only else 0,
            window=(settings.voice_local_context_window
                    - settings.voice_local_max_tokens
                    - estimate_tokens(json.dumps(tools))
                    - 512) if tools_only else None)

        async with db.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM tool_calls "
            "WHERE conversation_id = ?", (conversation_id,)) as cur:
            tools_before = (await cur.fetchone())["m"]

        final_content = ""
        # the ReAct loop runs INSIDE the guest; host tools brokered over vsock.
        # This is the only path — the host-side fallback went with M4e.
        envelope = TurnEnvelope(
            op_id=op_id, conversation_id=conversation_id, active_project=active,
            artifact_slug=(f"chat-{conversation_id}" if atoken is not None else None),
            web_session=runtime.web_session.get(), ephemeral=ephemeral,
            event_chan=chan,
            memory_slug=_memory_slug(agent_def) if agent_def else None)
        source = guest_turn(conversation_id, system_prompt, history,
                            rules=standing_rules_tail(), tool_specs=tools,
                            read_only=list(read_only_names(entries)),
                            op_id=op_id, envelope=envelope,
                            active_slug=active, push_workspace=True,
                            # voice turns skip the second-pass rules rewrite:
                            # the streamed text was already spoken aloud
                            rewrite_rules=not voice,
                            # ...and a voice turn on the LOCAL tier also skips
                            # restating the rules in the user turn: a 4B answers
                            # that text instead of obeying it. Escalated voice
                            # turns (DeepSeek, base_url unset) keep it.
                            inject_rules=not (voice and base_url),
                            # an agent definition may cap its own rounds; an
                            # orchestrator gets the longer monitoring cap (each
                            # plan_status wait is a round); None keeps the
                            # normal chat cap
                            max_iterations=((agent_def or {}).get("max_iterations")
                                            or (settings.orchestrator_max_iterations
                                                if orchestrating else None)),
                            # a chat thread is addressable — by its conversation
                            # id, and by its agent slug when WP4 bound one — and
                            # every chat turn takes the operator's mid-turn
                            # messages. An ephemeral turn drains ONLY the
                            # operator's (agentmsg.fetch_tool): agents cannot
                            # address it (send refuses, live_peers hides it),
                            # since a peer's message delivered into it would be
                            # marked delivered against a transcript the finally
                            # block is about to erase; the operator's own words
                            # being erased with the rest is the incognito promise.
                            inbox=True,
                            # approved /persist (if the operator approved this
                            # project) — never for incognito, which leaves no
                            # trace anywhere, a surviving disk included
                            persist=not ephemeral,
                            # voice local tier: run on the operator's ollama.
                            # The guest never dials it — the host gateway makes
                            # the call, so base_url is honoured host-side.
                            model_name=model_name, base_url=base_url)

        sink = db_tool_sink(db, conversation_id)
        pending_tool: dict = {}
        try:
            async for event in source:
                if event["type"] == "final":
                    final_content = event["content"]
                    continue
                # the guest loop runs with on_tool_call=None, so persist tool_calls
                # here by pairing each tool (args) event with its tool_result.
                if event["type"] == "tool":
                    pending_tool[event.get("id")] = (event.get("name"),
                                                     event.get("args") or {})
                elif event["type"] == "tool_result":
                    nm, ar = pending_tool.pop(event.get("id"), (event.get("name"), {}))
                    await sink(nm, ar, event.get("result", ""))
                bus.publish(chan, event)
        finally:
            # guest_turn holds a per-slug workspace-push token that its OWN
            # `finally` releases (and, for the last one out, sweeps the guest's
            # writes home). But an operator interrupt cancels this task while the
            # generator is suspended mid-turn, and Python does NOT finalize a
            # suspended async generator synchronously — so without an explicit
            # close that token leaks. A leaked token makes the NEXT top-level
            # turn skip its workspace push (acquire_workspace sees a phantom
            # concurrent holder), the guest then presents an EMPTY project, and
            # the agent concludes its work was wiped and rebuilds from scratch.
            # Closing here throws GeneratorExit in so that finally runs now. The
            # token release is synchronous (before any await), so the hold is
            # freed even if the close itself races the cancellation.
            try:
                await source.aclose()
            except Exception:  # noqa: BLE001 — the hold is already released
                pass

        # the loop is over, so nothing will drain the inbox again: whatever the
        # operator sent during the final answer goes back in `final`
        late = await agentmsg.close_operator_inbox(conversation_id)
        cur = await db.execute(
            "INSERT INTO messages (conversation_id, role, content, model) "
            "VALUES (?, 'assistant', ?, ?)",
            (conversation_id, final_content, model_name),
        )
        await _link_tool_calls(db, conversation_id, tools_before, cur.lastrowid)
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
                                    final_content, tools_before, active)
            except Exception:  # noqa: BLE001 — journaling never breaks a turn
                pass
        bus.publish(chan, _final_event(conversation_id, final_content, late))
    except asyncio.CancelledError:
        # the operator hit stop. Leave the interruption in the transcript
        # (persistent chats — the ephemeral wipe in finally covers incognito)
        # and give every tail a final event so the UI settles, then re-raise
        # so the task ends properly cancelled. A voice barge-in parks an
        # annotated note (what was actually heard) via set_interrupt_note;
        # without one this is the plain GUI stop marker.
        note = _interrupt_notes.pop(conversation_id, None)
        content = note if note is not None else INTERRUPTED_MARKER
        if not ephemeral:
            try:
                cur = await db.execute(
                    "INSERT INTO messages (conversation_id, role, content, model) "
                    "VALUES (?, 'assistant', ?, ?)",
                    (conversation_id, content, model_name))
                # a barge-in cancels the turn but the tools it already ran are
                # real — bind them to the marker so the next turn still sees
                # that acting happens through tool calls
                await _link_tool_calls(db, conversation_id, tools_before,
                                       cur.lastrowid)
                await db.commit()
            except Exception:  # noqa: BLE001 — the marker is best-effort
                pass
        # `late +`: the normal path may have closed already and then failed
        late = late + await agentmsg.close_operator_inbox(conversation_id)
        bus.publish(chan, _final_event(conversation_id, content, late))
        raise
    except Exception as exc:  # surfaced to any tail rather than lost
        err = {"type": "error", "message": str(exc)}
        late = late + await agentmsg.close_operator_inbox(conversation_id)
        if late:
            err["undelivered"] = late
        bus.publish(chan, err)
    finally:
        # normally already closed above; this covers a path that raised
        # before reaching the close (the rows then wait for the next turn)
        agentmsg.forget_operator_inbox(conversation_id)
        if db is not None and ephemeral:
            # incognito: no trace in the DB or GUI — but the operator asked
            # for an SSH-only recovery hatch, so the turn's transcript is
            # appended to a date-stamped file under data/ (gitignored, never
            # served) before the wipe. Best-effort: a dump failure must not
            # keep the rows alive.
            try:
                async with db.execute(
                    "SELECT role, content, created_at FROM messages "
                    "WHERE conversation_id = ? ORDER BY id",
                    (conversation_id,)) as cur:
                    msgs = await cur.fetchall()
                if msgs:
                    dump_dir = settings.data_dir / "incognito"
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    path = dump_dir / f"{msgs[0]['created_at'][:10]}.md"
                    with path.open("a", encoding="utf-8") as fh:
                        fh.write(f"\n---\n\n## chat {conversation_id} · "
                                 f"{msgs[-1]['created_at']} UTC\n\n")
                        for m in msgs:
                            fh.write(f"**{m['role']}**:\n\n{m['content']}\n\n")
            except Exception:  # noqa: BLE001 — recovery dump is best-effort
                pass
            # an incognito turn can still spawn an agent or launch a job, whose
            # row points back here; without this the DELETE below raises a FK
            # error inside this finally, skipping the contextvar resets, the
            # _active_turns eviction and bus.close_job — bricking the chat
            await _drop_references(db, conversation_id)
            for tbl in ("tool_calls", "messages", "conversations"):
                col = "id" if tbl == "conversations" else "conversation_id"
                await db.execute(f"DELETE FROM {tbl} WHERE {col} = ?", (conversation_id,))
            await db.commit()
            shutil.rmtree(settings.memory_dir / ".ephemeral-notes", ignore_errors=True)
        elif db is not None:
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
        if ptoken is not None:
            runtime.active_project.reset(ptoken)
        runtime.gui_tab.reset(tabtoken)
        runtime.conversation_id.reset(cidtoken)
        runtime.web_session.reset(wtoken)
        runtime.event_chan.reset(ctoken)
        runtime.ephemeral.reset(token)
        budget.active_op_id.reset(optoken)
        budget.release(op_id)
        if db is not None:
            await db.close()
        # order matters for the reconnect race: drop the running flag, THEN
        # signal end — a subscriber that still sees the flag is guaranteed
        # the job_end is ahead of it in the queue (both happen in this tick)
        _active_turns.pop(conversation_id, None)
        _turn_actors.pop(conversation_id, None)
        _interrupt_notes.pop(conversation_id, None)   # stale note must not leak
        bus.close_job(chan)


def _final_event(conversation_id: int, content: str, late: list[str]) -> dict:
    """A turn's closing event. `undelivered` is present only when the operator
    sent messages (POST /api/chat/{cid}/message) that the turn ended without
    reading — the client sends them on as the next turn."""
    ev = {"type": "final", "content": content, "conversation_id": conversation_id}
    if late:
        ev["undelivered"] = late
    return ev


def _tail(conversation_id: int, q, chan: str | None = None) -> "StreamingResponse":
    """SSE-forward a conversation's bus channel until the turn ends. Client
    disconnect cancels only this tail, never the turn."""
    chan = chan or _chan(conversation_id)

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


def _idle() -> StreamingResponse:
    async def idle():
        yield sse({"type": "idle"})
    return StreamingResponse(idle(), media_type="text/event-stream")


@router.get("/chat/{conversation_id}/stream")
async def resume_chat_stream(conversation_id: int):
    """Re-attach to an in-flight turn (page reload, coming back to the tab).
    Tokens streamed before attaching are gone, but the final event carries the
    complete reply, so the GUI ends up whole either way.

    Any conversation id works, not only a chat's: a spawned agent, a plan item,
    a funnel node or a job head is tailed the same way (_stream_node)."""
    q = bus.subscribe(_chan(conversation_id))
    if conversation_id not in _active_turns:
        # subscribe-then-check closes the race with the turn's finally block
        bus.unsubscribe(_chan(conversation_id), q)
        return await _stream_node(conversation_id)
    return _tail(conversation_id, q)


@router.get("/chat/agents/{conversation_id}/stream")
async def agent_node_stream(conversation_id: int):
    """Tail any node of the agents tree by conversation id — the same SSE
    contract as a chat turn (token / tool / tool_result / final / error, or
    one `idle` when nothing is running there). Same as
    /api/chat/{id}/stream; spelled under /agents for clients that read the
    tree and want the obvious URL."""
    return await resume_chat_stream(conversation_id)


async def _stream_node(cid: int) -> StreamingResponse:
    """A conversation that is not a running chat turn: an agent/job turn
    (vm/turn.py publishes every event on node:<cid>), or a job head, which runs
    no loop of its own and is tailed through its job's channel instead."""
    from .vm import turn as vm_turn
    chan = vm_turn.node_chan(cid)
    q = bus.subscribe(chan)
    if cid in vm_turn.live_nodes():
        return _tail(cid, q, chan)
    bus.unsubscribe(chan, q)
    db = await get_db()
    try:
        async with db.execute(
            "SELECT kind, job_id, rollup FROM conversations WHERE id = ?",
            (cid,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    if row and row["kind"] == "head" and _head_running(row):
        return _tail_head(cid, row["job_id"])
    return _idle()


def _head_running(row) -> bool:
    """A job head is running while it has no rollup AND its job still holds a
    Budget. The rollup alone is what runs_api reads, but a job the process lost
    in a restart never writes one and would read as running forever; every
    job runner (funnel, research, plan) registers its Budget under the job id
    for exactly the job's life."""
    from .agent import budget as budget_mod
    return row["rollup"] is None and budget_mod.get(row["job_id"]) is not None


def _tail_head(cid: int, job_id: str) -> StreamingResponse:
    """A head's view of its job, in the chat event contract: the job's own
    events pass through (node_spawned / node_status / plan_item / tool with a
    node_id ...), and the job's end becomes the head's `final` carrying the
    rollup, so a client that only knows the chat contract still settles."""
    q = bus.subscribe(job_id)

    async def event_stream():
        try:
            while True:
                ev = await q.get()
                t = ev.get("type")
                if t == "job_end":
                    break
                if t == "job_final":
                    yield sse({"type": "final", "content": ev.get("rollup") or "",
                               "conversation_id": cid})
                    break
                if t == "token":
                    continue      # leaves' token firehose: not the head's words
                yield sse(ev)
        finally:
            bus.unsubscribe(job_id, q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/chat/{conversation_id}/message")
async def operator_message(conversation_id: int, body: OperatorMessage):
    """The operator talking into a turn that is already running — a chat, or
    any agent/job node with a loop (a spawned agent, a plan item, a funnel
    leaf). The words go into that turn's inbox (agentmsg: the same drain as
    agent mail, framed as the operator) and reach the model at its next
    reasoning round, after the current tool call returns. At delivery every
    attached stream gets {"type": "operator_message", "text"} and the words
    land in the transcript as a user message, in order.

    409 no_turn_running when nothing there will read it — no turn, a job head
    (it runs no loop), or a turn that just finished: the client then starts a
    normal turn. A message that arrives too late for a turn that is ending
    comes back on that turn's `final` as `undelivered`."""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > agentmsg.OPERATOR_MAX_BODY:
        raise HTTPException(status_code=400,
                            detail=f"text is over {agentmsg.OPERATOR_MAX_BODY} characters")
    if not await agentmsg.queue_operator_message(conversation_id, text):
        raise HTTPException(status_code=409, detail="no_turn_running")
    return {"queued": True}


# how many of the newest roots the agents tree shows, besides every running one
AGENT_TREE_ROOTS = 50

# a conversation is a root of the agents tree when it has no parent and is
# agent work of some kind: an orchestrator, a job/agent node, an agent thread,
# or an ordinary chat that spawned agent work (it is shown as the root). A
# plain chat that spawned nothing is not agent work and stays in the sidebar.
_TREE_ROOT_SQL = """
SELECT c.id FROM conversations c
WHERE c.parent_conversation_id IS NULL AND c.ephemeral = 0
  AND (c.mode = 'orchestrate' OR c.kind != 'chat' OR c.agent_slug IS NOT NULL
       OR EXISTS (SELECT 1 FROM conversations k
                  WHERE k.parent_conversation_id = c.id AND k.kind != 'chat'))
"""

# ...and its nodes: the roots plus every descendant that is not itself a plain
# chat (voice continues a conversation as a kind='chat' child; that is the same
# conversation carried on, not an agent). UNION, not UNION ALL, so a malformed
# parent cycle terminates.
_TREE_SQL = """
WITH RECURSIVE tree(id) AS (
    SELECT value FROM json_each(?)
    UNION
    SELECT c.id FROM conversations c JOIN tree t ON c.parent_conversation_id = t.id
    WHERE c.kind != 'chat' AND c.ephemeral = 0
)
SELECT c.id, c.parent_conversation_id AS parent_id, c.kind, c.mode,
       c.summary AS title, c.agent_slug, c.model, c.started_at, c.job_id,
       c.rollup, p.slug AS project
FROM conversations c JOIN tree t ON t.id = c.id
LEFT JOIN projects p ON p.id = c.project_id
"""

# walk up from a running node to its root
_ROOT_OF_SQL = """
WITH RECURSIVE up(id, parent, d) AS (
    SELECT id, parent_conversation_id, 0 FROM conversations WHERE id = ?
    UNION ALL
    SELECT c.id, c.parent_conversation_id, up.d + 1 FROM conversations c
    JOIN up ON c.id = up.parent WHERE up.d < 32
)
SELECT id FROM up ORDER BY d DESC LIMIT 1
"""


def _running_loops() -> set[int]:
    """Every conversation with a loop in flight: chat turns, interactive agent
    runs, agent/job turns (vm/turn.py), and anything else the broker holds a
    live envelope for."""
    from . import agents_run
    from .vm import broker
    from .vm import turn as vm_turn
    ids = set(_active_turns) | set(agents_run._active_runs) | vm_turn.live_nodes()
    ids |= {e.conversation_id for e in broker.live_turns()
            if e.conversation_id and not e.ephemeral}
    return ids


@router.get("/chat/agents")
async def agents_tree():
    """Every orchestrator, agent and job across all projects, as one flat list
    of nodes the client nests by parent_id: everything running, plus the
    newest AGENT_TREE_ROOTS roots with their whole subtrees. Roots come first,
    newest first, each followed by its descendants depth-first."""
    live = _running_loops()
    db = await get_db()
    try:
        async with db.execute(
            _TREE_ROOT_SQL + " ORDER BY c.started_at DESC, c.id DESC LIMIT ?",
            (AGENT_TREE_ROOTS,)) as cur:
            roots = [r["id"] for r in await cur.fetchall()]
        # a running head is live without a loop of its own
        async with db.execute(
            "SELECT id, job_id, rollup FROM conversations "
            "WHERE kind = 'head' AND rollup IS NULL") as cur:
            heads = {r["id"] for r in await cur.fetchall() if _head_running(r)}
        live |= heads
        for cid in sorted(live):
            async with db.execute(_ROOT_OF_SQL, (cid,)) as cur:
                r = await cur.fetchone()
            if r and r["id"] not in roots:
                roots.append(r["id"])
        async with db.execute(_TREE_SQL, (json.dumps(roots),)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    # a root pulled in only because something under it runs must itself be agent
    # work or have agent work under it — a running plain chat is not a node
    by_id = {r["id"]: r for r in rows}
    kids: dict[int, list[dict]] = {}
    for r in rows:
        if r["parent_id"] in by_id and r["id"] not in roots:
            kids.setdefault(r["parent_id"], []).append(r)
    ordered = sorted((by_id[i] for i in roots if i in by_id),
                     key=lambda r: (r["started_at"] or "", r["id"]), reverse=True)
    out: list[dict] = []

    def walk(r: dict) -> None:
        if r["kind"] == "chat" and not r["mode"] and not r["agent_slug"] \
                and r["id"] not in kids:
            return
        out.append({
            "id": r["id"],
            "parent_id": r["parent_id"] if r["parent_id"] in by_id else None,
            "kind": "orchestrator" if r["mode"] == "orchestrate" else r["kind"],
            "title": r["title"] or "", "agent_slug": r["agent_slug"],
            "project": r["project"], "model": r["model"],
            "running": r["id"] in live, "started_at": r["started_at"]})
        for k in sorted(kids.get(r["id"], ()), key=lambda k: k["id"]):
            walk(k)

    for r in ordered:
        walk(r)
    return {"nodes": out}


@router.post("/chat/{conversation_id}/stop")
async def stop_chat_turn(conversation_id: int):
    """Cancel an in-flight turn. The turn's CancelledError handler records
    the interruption and publishes a final event, so every attached tail
    (and the transcript) settles on its own — nothing else to clean up here."""
    return {"stopped": _stop(conversation_id)}


def _stop(conversation_id: int) -> bool:
    task = _active_turns.get(conversation_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


# Who started each in-flight turn: "device:<token id>" for a `jav3` device
# token, "session" for the operator's cookie. require_actor runs once, when the
# POST arrives, and the turn it admits is a detached task — so revoking a device
# token must also stop what that token already started (devices_api calls
# stop_actor_turns on every revoke).
_turn_actors: dict[int, str] = {}


def device_actor(token_id: int) -> str:
    return f"device:{int(token_id)}"


def actor_key(actor: dict | None) -> str | None:
    if not actor:
        return None
    return device_actor(actor["device_id"]) if actor.get("is_device") else "session"


def stop_actor_turns(key: str) -> int:
    """Cancel every in-flight turn `key` started, through the same path as
    /stop. Returns how many were cancelled."""
    return sum(_stop(cid) for cid, who in list(_turn_actors.items()) if who == key)


@router.post("/chat")
async def chat(body: ChatRequest, actor: dict = Depends(require_actor)):
    # the router already depends on require_actor; FastAPI caches it per
    # request, so this is the same resolved actor, not a second token lookup
    device_id = actor.get("device_id") if actor.get("is_device") else None
    pinned_model = None
    if body.model:
        try:
            pinned_model = providers.checked(body.model)
        except providers.ProviderError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
    db = await get_db()
    try:
        conversation_id = body.conversation_id
        if conversation_id is not None and conversation_id in _active_turns:
            raise HTTPException(status_code=409, detail="turn_in_progress")
        if conversation_id is None:
            # Peak-cost gate (spec §4) BEFORE the conversation exists: the old
            # order created the row first, so this 409 left an orphan,
            # blank-rendering conversation behind (and the retry opened a
            # fresh one — twin entries in the sidebar). DeepSeek hours only.
            if (in_peak_window() and not body.confirm_peak
                    and providers.peak_priced(pinned_model)):
                raise HTTPException(status_code=409,
                                    detail="peak_confirmation_required")
            # identity is validated here, not in the detached turn: a typo'd
            # slug is a 404 on the POST the operator can see, not an error
            # event on a conversation that already exists
            if body.mode == "orchestrate":
                # an orchestrator's agents all work one project on this host,
                # under that project's egress policy — so it must name one up
                # front rather than follow whatever happens to be loaded
                if not body.project:
                    raise HTTPException(status_code=400,
                                        detail="mode orchestrate needs a project")
                if body.project_mode not in (None, "pin"):
                    raise HTTPException(status_code=400,
                                        detail="mode orchestrate pins its project")
                if body.ephemeral:
                    # a plan is a saved file and a team of recorded runs
                    raise HTTPException(status_code=400,
                                        detail="mode orchestrate cannot be a temporary chat")
                if body.agent:
                    raise HTTPException(status_code=400,
                                        detail="mode orchestrate runs as Jav3, not an agent")
            agent_def = None
            if body.agent:
                from .agents_api import _read as read_agent_def
                agent_def = read_agent_def(body.agent)     # 404s on an unknown slug
            mode = body.project_mode or ("pin" if body.project else "follow")
            if (agent_def is not None and body.project_mode is None
                    and not body.project):
                # precedence: request > the definition's `project` > follow.
                # An agent that lives in a project opens its threads there.
                from .agents_run import bound_project
                bound = await bound_project(db, agent_def)
                if bound:
                    mode, body.project = "pin", bound
            if mode == "pin" and body.project:
                async with db.execute(
                    "SELECT 1 FROM projects WHERE slug = ? AND deleted_at IS NULL",
                    (body.project,)) as cur:
                    if not await cur.fetchone():
                        raise HTTPException(status_code=404,
                                            detail=f"no such project: {body.project}")
                active = body.project
            elif mode == "none":
                active = None      # deliberately unbound: artifacts, not a project
            else:
                active = await get_active_project(db)
            # provisional title: first bit of the opening message; an LLM
            # naming pass upgrades it after the first exchange (best effort)
            title = " ".join(body.message.split())[:48] or "(empty)"
            conversation_id = await open_conversation(
                db, project=active, title=title, locked=mode != "follow",
                agent=body.agent or None,
                # persist the incognito marker on the row itself: it is the
                # source of truth agentmsg._is_incognito reads to refuse a
                # message to a turn about to be wiped, and it outlives the
                # broker envelope that carries the same flag. Gone with the row
                # at turn end.
                ephemeral=body.ephemeral,
                # which computer opened this thread (NULL = the operator)
                device_id=device_id,
                mode=body.mode)
            if body.confirm_peak:
                confirm_peak(conversation_id)
        else:
            async with db.execute(
                "SELECT model FROM conversations WHERE id = ?", (conversation_id,)
            ) as cur:
                existing = await cur.fetchone()
                if not existing:
                    raise HTTPException(status_code=404, detail="no such conversation")
            # Peak-cost gate for an existing conversation: confirmation is
            # keyed to its id, so it can (and must) be checked after lookup.
            if body.confirm_peak:
                confirm_peak(conversation_id)
            if (in_peak_window() and not peak_confirmed(conversation_id)
                    and providers.peak_priced(pinned_model or existing["model"])):
                raise HTTPException(
                    status_code=409,
                    detail="peak_confirmation_required",
                    headers={"X-Conversation-Id": str(conversation_id)},
                )

        if pinned_model:
            await db.execute("UPDATE conversations SET model = ? WHERE id = ?",
                             (pinned_model, conversation_id))
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
    start_turn(conversation_id, ephemeral=body.ephemeral,
               user_msg=body.message, tab=body.tab, actor=actor_key(actor))
    return _tail(conversation_id, q)


def start_turn(conversation_id: int, *, ephemeral: bool = False,
               user_msg: str = "", tab: str | None = None,
               voice: bool = False, model_name: str | None = None,
               base_url: str | None = None,
               context_exclude: tuple = (),
               tools_only: tuple = (),
               actor: str | None = None) -> asyncio.Task:
    """Launch a chat turn as a detached task. The one shared seam between the
    HTTP endpoint above and the voice orchestrator: the caller has already
    inserted the user message row, run the peak gate, and (if it wants the
    early events) subscribed to the conversation's bus channel."""
    task = asyncio.create_task(
        _run_chat_turn(conversation_id, ephemeral, user_msg, tab, voice=voice,
                       model_name=model_name, base_url=base_url,
                       context_exclude=context_exclude, tools_only=tools_only))
    _active_turns[conversation_id] = task
    # open alongside the running flag, in the same tick: from here the turn
    # takes operator messages (POST /api/chat/{cid}/message) until it closes
    # its inbox after its last drain
    agentmsg.open_operator_inbox(conversation_id)
    if actor:
        _turn_actors[conversation_id] = actor
    return task
