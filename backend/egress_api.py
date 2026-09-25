"""GUI + agent API for monitored egress and security alerts (A5 / C1 backends).

Two routers:
  /api/egress   — the live network feed, per-project policy, the host-approval
                  queue that trains the allowlist up, and per-project secret grants.
  /api/security — the persisted, acknowledgeable security-alert store.
Both expose an SSE `/stream` fed from the in-process bus, mirroring the Runs tab.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import bus, egress, egress_auto, secctx, security
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
        q = ("SELECT id, project_slug, host, method, path, bytes_out, bytes_in, verdict, "
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


class BulkBody(BaseModel):
    action: str                    # approve | reject | dismiss
    project: str | None = None     # limit to one project's queue


@router.post("/pending/bulk")
async def bulk_pending(body: BulkBody):
    db = await get_db()
    try:
        return await egress.bulk_pending(db, body.action, body.project)
    finally:
        await db.close()


@router.get("/summary")
async def summary(project: str | None = None):
    """The Network page's three counts: distinct hosts allowed / denied in the
    last 24h, and hosts waiting on the operator now."""
    db = await get_db()
    try:
        where, args = "created_at > datetime('now', '-1 day')", ()
        if project:
            where += " AND project_slug = ?"
            args = (project,)
        async with db.execute(
                "SELECT COUNT(DISTINCT CASE WHEN verdict IN ('allow','auto_allow') "
                "THEN host END) AS allowed, "
                "COUNT(DISTINCT CASE WHEN verdict IN ('deny','auto_deny','cut') "
                f"THEN host END) AS denied FROM egress_events WHERE {where}", args) as cur:
            r = dict(await cur.fetchone())
        r["waiting"] = len(await egress.list_pending(db, project))
        return r
    finally:
        await db.close()


# --- egress: auto mode (test only; off by default) ---------------------------

class AutoBody(BaseModel):
    mode: str                      # on | off | inherit (inherit: per project only)
    project: str | None = None     # None / '' = the global default


@router.get("/auto")
async def get_auto(project: str | None = None):
    db = await get_db()
    try:
        return await egress_auto.get_mode(db, project)
    finally:
        await db.close()


@router.put("/auto")
async def put_auto(body: AutoBody):
    db = await get_db()
    try:
        res = await egress_auto.set_mode(db, body.project, body.mode)
    finally:
        await db.close()
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


# --- egress: the standing allowlists (with where each entry came from) -------

@router.get("/allowlist")
async def allowlist():
    db = await get_db()
    try:
        return {"groups": await egress.allowlist(db)}
    finally:
        await db.close()


class RevokeBody(BaseModel):
    project: str = ""              # the list's own slug ('__general__' = shared)
    host: str = ""
    id: int | None = None          # an auto entry's id (source='auto')


@router.post("/allowlist/revoke")
async def revoke(body: RevokeBody):
    db = await get_db()
    try:
        if body.id is not None:
            res = await egress.revoke_auto(db, body.id)
        else:
            res = await egress.remove_host(db, body.project, body.host)
    finally:
        await db.close()
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res["error"])
    return res


@router.post("/auto/{aid}/promote")
async def promote(aid: int):
    db = await get_db()
    try:
        res = await egress.promote_auto(db, aid)
    finally:
        await db.close()
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res["error"])
    return res


class AllowBody(BaseModel):
    project: str = ""
    host: str


@router.post("/allow")
async def allow(body: AllowBody):
    """Operator override for a host that is not in the waiting queue (an
    auto-deny): allow it for this project the way an approval would."""
    db = await get_db()
    try:
        res = await egress.allow_host(db, body.project, body.host)
    finally:
        await db.close()
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


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


@security_router.get("/events/{eid}/context")
async def event_context(eid: int):
    """The evidence board for one alert — the flagged code in place, the diff,
    the directory, the traffic. Reachable for acknowledged events too, so a
    toast still opens something days later."""
    db = await get_db()
    try:
        ev = await security.get_event(db, eid)
        if ev is None:
            raise HTTPException(status_code=404, detail="no such event")
        return await secctx.build_board(db, ev)
    finally:
        await db.close()


@security_router.post("/events/{eid}/ack")
async def ack(eid: int):
    db = await get_db()
    try:
        return await security.acknowledge(db, eid)
    finally:
        await db.close()


@security_router.post("/events/ack_all")
async def ack_all():
    db = await get_db()
    try:
        return await security.acknowledge_all(db)
    finally:
        await db.close()


@security_router.get("/stream")
async def security_stream():
    return await _channel_stream(security.SECURITY_CHAN)
