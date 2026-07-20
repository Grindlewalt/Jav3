"""GUI + agent API for monitored egress and security alerts (A5 / C1 backends).

Two routers:
  /api/egress   — the live network feed, per-project policy, the host-approval
                  queue that trains the allowlist up, and per-project secret grants.
  /api/security — the persisted, acknowledgeable security-alert store.
Both expose an SSE `/stream` fed from the in-process bus, mirroring the Runs tab.
"""
import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import bus, egress, security
from .auth import require_user
from .db import get_db

router = APIRouter(prefix="/api/egress", tags=["egress"],
                   dependencies=[Depends(require_user)])
security_router = APIRouter(prefix="/api/security", tags=["security"],
                            dependencies=[Depends(require_user)])


def _sse(d: dict) -> str:
    return f"data: {json.dumps(d)}\n\n"


async def _channel_stream(channel: str):
    queue = bus.subscribe(channel)

    async def gen():
        try:
            yield _sse({"type": "stream_open", "channel": channel})
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=25)
                    yield _sse(ev)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"           # keep the connection warm
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(channel, queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


# --- egress: live feed -------------------------------------------------------

@router.get("/events")
async def recent_events(limit: int = 200, project: str | None = None):
    db = await get_db()
    try:
        q = ("SELECT project_slug, host, method, path, bytes_out, bytes_in, verdict, "
             "reason, created_at FROM egress_events")
        args: tuple = ()
        if project:
            q += " WHERE project_slug = ?"
            args = (project,)
        q += " ORDER BY id DESC LIMIT ?"
        async with db.execute(q, (*args, limit)) as cur:
            return {"events": [dict(r) for r in await cur.fetchall()]}
    finally:
        await db.close()


@router.get("/stream")
async def egress_stream():
    return await _channel_stream(egress.EGRESS_CHAN)


# --- egress: approval queue (trains the allowlist up) ------------------------

@router.get("/pending")
async def pending(project: str | None = None):
    db = await get_db()
    try:
        return {"pending": await egress.list_pending(db, project)}
    finally:
        await db.close()


@router.post("/pending/{pid}/approve")
async def approve(pid: int):
    db = await get_db()
    try:
        return await egress.approve_host(db, pid)
    finally:
        await db.close()


@router.post("/pending/{pid}/reject")
async def reject(pid: int):
    db = await get_db()
    try:
        return await egress.reject_host(db, pid)
    finally:
        await db.close()


# --- egress: per-project policy ---------------------------------------------

class PolicyBody(BaseModel):
    mode: str = "allowlist"
    inherit_general: bool = True
    hosts: list[str] = []


@router.get("/policy/{slug}")
async def get_policy(slug: str):
    db = await get_db()
    try:
        return await egress.get_policy(db, slug)
    finally:
        await db.close()


@router.put("/policy/{slug}")
async def put_policy(slug: str, body: PolicyBody):
    db = await get_db()
    try:
        return await egress.set_policy(db, slug, mode=body.mode,
                                       inherit_general=body.inherit_general, hosts=body.hosts)
    finally:
        await db.close()


# --- egress: per-project secret grants (Layer 2) ----------------------------

class GrantBody(BaseModel):
    secret: str
    status: str = "granted"


@router.get("/grants/{slug}")
async def list_grants(slug: str):
    db = await get_db()
    try:
        return {"grants": await egress.project_secrets(db, slug)}
    finally:
        await db.close()


@router.post("/grants/{slug}")
async def set_grant(slug: str, body: GrantBody):
    db = await get_db()
    try:
        return await egress.grant_secret(db, slug, body.secret, status=body.status)
    finally:
        await db.close()


# --- security alerts ---------------------------------------------------------

@security_router.get("/events")
async def security_events(unacknowledged: bool = False, limit: int = 100):
    db = await get_db()
    try:
        return {"events": await security.list_events(
            db, unacknowledged_only=unacknowledged, limit=limit)}
    finally:
        await db.close()


@security_router.post("/events/{eid}/ack")
async def ack(eid: int):
    db = await get_db()
    try:
        return await security.acknowledge(db, eid)
    finally:
        await db.close()


@security_router.get("/stream")
async def security_stream():
    return await _channel_stream(security.SECURITY_CHAN)
