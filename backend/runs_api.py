"""Agent-run (job) API: launch a research job and stream its tree live, plus
read past run trees. The live stream is the "watch the bots" view.

POST /api/runs/research starts the job as a background task and streams the
in-process bus events for that job_id; GET endpoints walk the retained tree
(cascading fidelity: shallow by default, ?depth=full to expand a branch).
"""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import bus, research
from .agent.model import in_peak_window, peak_confirmed
from .auth import require_user
from .db import get_db
from .memory import get_active_project

router = APIRouter(prefix="/api/runs", tags=["runs"], dependencies=[Depends(require_user)])


def sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


class ResearchRun(BaseModel):
    topic: str
    angles: int = 4
    confirm_peak: bool = False


@router.post("/research")
async def research_run(body: ResearchRun):
    if not body.topic.strip():
        raise HTTPException(status_code=400, detail="topic is required")
    db = await get_db()
    try:
        project = await get_active_project(db)
    finally:
        await db.close()
    if not project:
        raise HTTPException(status_code=400, detail="load a project first — research stages into it")

    import uuid
    job_id = uuid.uuid4().hex  # minted here so we subscribe before the task runs

    # Peak gate once for the whole job (nodes auto-confirm once greenlit).
    if not body.confirm_peak and in_peak_window() and not peak_confirmed(job_id):
        raise HTTPException(status_code=409, detail="peak_confirmation_required",
                            headers={"X-Conversation-Id": job_id})

    queue = bus.subscribe(job_id)
    task = asyncio.create_task(
        research.run_research(body.topic, project, n_angles=body.angles, job_id=job_id))

    async def event_stream():
        try:
            yield sse({"type": "job_opened", "job_id": job_id})
            while True:
                event = await queue.get()
                if event is bus.JOB_END or event.get("type") == "job_end":
                    break
                yield sse(event)
        except asyncio.CancelledError:  # client disconnected
            pass
        finally:
            bus.unsubscribe(job_id, queue)
            # the job keeps running to completion regardless of the socket
            if not task.done():
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("")
async def list_runs():
    db = await get_db()
    try:
        async with db.execute(
            "SELECT c.id, c.summary, c.job_id, c.started_at, c.rollup, p.slug AS project_slug "
            "FROM conversations c LEFT JOIN projects p ON p.id = c.project_id "
            "WHERE c.kind = 'head' ORDER BY c.started_at DESC LIMIT 100") as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()
    return {"runs": [dict(r) for r in rows]}


@router.get("/{cid}/tree")
async def run_tree(cid: int, depth: str = "shallow"):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT id, kind, summary, rollup, parent_conversation_id, job_id "
            "FROM conversations WHERE id = ?", (cid,)) as cur:
            node = await cur.fetchone()
        if node is None:
            raise HTTPException(status_code=404, detail="no such run node")
        if depth == "full":
            async with db.execute(
                "SELECT id, kind, summary, rollup, parent_conversation_id, job_id "
                "FROM conversations WHERE job_id = ? ORDER BY id", (node["job_id"],)) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT id, kind, summary, rollup, parent_conversation_id, job_id "
                "FROM conversations WHERE parent_conversation_id = ? ORDER BY id", (cid,)) as cur:
                children = await cur.fetchall()
            rows = [node, *children]
    finally:
        await db.close()
    return {"nodes": [dict(r) for r in rows]}
