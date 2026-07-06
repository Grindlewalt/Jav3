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
    # a head with no rollup yet is still running (watchable live)
    return {"runs": [{**dict(r), "running": r["rollup"] is None} for r in rows]}


def _depths(nodes) -> dict:
    parent = {n["id"]: n["parent_conversation_id"] for n in nodes}
    out = {}
    for nid in parent:
        d, p = 0, parent[nid]
        while p is not None and p in parent:
            d += 1
            p = parent[p]
        out[nid] = d
    return out


@router.get("/{cid}/stream")
async def run_stream(cid: int):
    """Follow a job live by its head id — including one Jarvis deployed from a
    chat. Emits the tree-so-far as a snapshot, then streams live bus events
    until the job ends (or, if it already finished, closes right after)."""
    db = await get_db()
    try:
        async with db.execute(
            "SELECT job_id, rollup FROM conversations WHERE id = ? AND kind = 'head'",
            (cid,)) as cur:
            head = await cur.fetchone()
        if head is None:
            raise HTTPException(status_code=404, detail="no such run")
        job_id = head["job_id"]
        async with db.execute(
            "SELECT id, kind, summary, rollup, parent_conversation_id "
            "FROM conversations WHERE job_id = ? ORDER BY id", (job_id,)) as cur:
            nodes = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()

    done_at_start = head["rollup"] is not None
    depths = _depths(nodes)
    queue = bus.subscribe(job_id)  # subscribe before emitting snapshot: no gap

    async def event_stream():
        try:
            for n in nodes:  # snapshot of what already happened
                title = (n["summary"] or "").split("] ", 1)[-1]
                yield sse({"type": "node_spawned", "node_id": n["id"],
                           "parent_id": n["parent_conversation_id"], "kind": n["kind"],
                           "title": title, "depth": depths.get(n["id"], 0)})
                if n["rollup"] is not None:
                    yield sse({"type": "node_done", "node_id": n["id"], "rollup": n["rollup"]})
            if done_at_start:
                yield sse({"type": "job_final", "job_id": job_id, "root_id": cid})
                return
            while True:  # follow live
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    d2 = await get_db()  # did it finish while we waited?
                    try:
                        async with d2.execute(
                            "SELECT rollup FROM conversations WHERE id = ?", (cid,)) as cur:
                            r = await cur.fetchone()
                    finally:
                        await d2.close()
                    if r and r["rollup"] is not None:
                        yield sse({"type": "job_final", "job_id": job_id, "root_id": cid})
                        break
                    continue
                if event is bus.JOB_END or event.get("type") == "job_end":
                    break
                yield sse(event)
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(job_id, queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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
