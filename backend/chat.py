import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .agent.model import confirm_peak, in_peak_window, peak_confirmed
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


def sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


@router.get("/conversations")
async def list_conversations(project: str | None = None):
    db = await get_db()
    try:
        if project:
            q = ("SELECT c.* FROM conversations c JOIN projects p ON p.id = c.project_id "
                 "WHERE p.slug = ? ORDER BY c.started_at DESC")
            params: tuple = (project,)
        else:
            q = "SELECT * FROM conversations ORDER BY started_at DESC"
            params = ()
        async with db.execute(q, params) as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()
    return {"conversations": [dict(r) for r in rows]}


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
            cur = await db.execute(
                "INSERT INTO conversations (project_id) VALUES (?)", (project_id,))
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
            yield sse({"type": "final", "content": final_content,
                       "conversation_id": conversation_id})
        except Exception as exc:  # surfaced to the GUI rather than a dropped stream
            yield sse({"type": "error", "message": str(exc)})
        finally:
            await db.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")
