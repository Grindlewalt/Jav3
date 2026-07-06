import asyncio
import json
import shutil

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import runtime
from .agent.model import confirm_peak, in_peak_window, model, peak_confirmed
from .agent.loop import run_turn
from .auth import require_user
from .config import settings
from .db import get_db
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
    return {"conversations": [dict(r) for r in rows]}


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
            rows = await cur.fetchall()
    finally:
        await db.close()
    return {"messages": [dict(r) for r in rows]}


@router.post("/chat")
async def chat(body: ChatRequest):
    db = await get_db()
    try:
        conversation_id = body.conversation_id
        if conversation_id is None:
            active = await get_active_project(db)
            project_id = None
            if active:
                async with db.execute(
                    "SELECT id FROM projects WHERE slug = ?", (active,)
                ) as cur:
                    row = await cur.fetchone()
                project_id = row["id"] if row else None
            # provisional title: first bit of the opening message; an LLM
            # naming pass upgrades it after the first exchange (best effort)
            title = " ".join(body.message.split())[:48] or "(empty)"
            cur = await db.execute(
                "INSERT INTO conversations (project_id, summary) VALUES (?, ?)",
                (project_id, title))
            conversation_id = cur.lastrowid
            await db.commit()
        else:
            async with db.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ) as cur:
                if not await cur.fetchone():
                    raise HTTPException(status_code=404, detail="no such conversation")

        # Peak-cost gate (spec §4): inside a peak window the user must opt in.
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

    async def event_stream():
        token = runtime.ephemeral.set(body.ephemeral)
        db = await get_db()
        try:
            yield sse({"type": "start", "conversation_id": conversation_id})
            system_prompt = await assemble_system_prompt(db)
            async with db.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? "
                "ORDER BY id DESC LIMIT ?", (conversation_id, settings.recent_message_limit)
            ) as cur:
                rows = await cur.fetchall()
            history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

            final_content = ""
            async for event in run_turn(db, conversation_id, system_prompt, history):
                if event["type"] == "final":
                    final_content = event["content"]
                else:
                    yield sse(event)

            await db.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'assistant', ?)",
                (conversation_id, final_content),
            )
            await db.commit()
            if not body.ephemeral:
                async with db.execute(
                    "SELECT COUNT(*) AS c FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                ) as cur:
                    count = (await cur.fetchone())["c"]
                if count == 2:  # first exchange done — try to give it a real name
                    asyncio.create_task(
                        _name_conversation(conversation_id, body.message, final_content))
            yield sse({"type": "final", "content": final_content,
                       "conversation_id": conversation_id})
        except Exception as exc:  # surfaced to the GUI rather than a dropped stream
            yield sse({"type": "error", "message": str(exc)})
        finally:
            if body.ephemeral:
                # incognito: leave zero trace — drop the convo and any temp notes
                for tbl in ("tool_calls", "messages", "conversations"):
                    col = "id" if tbl == "conversations" else "conversation_id"
                    await db.execute(f"DELETE FROM {tbl} WHERE {col} = ?", (conversation_id,))
                await db.commit()
                shutil.rmtree(settings.memory_dir / ".ephemeral-notes", ignore_errors=True)
            runtime.ephemeral.reset(token)
            await db.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")
