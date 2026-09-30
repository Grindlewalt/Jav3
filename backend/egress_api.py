"""GUI + agent API for monitored egress and security alerts (A5 / C1 backends).

Two routers:
  /api/egress   — the live network feed, per-project policy, the host-approval
                  queue that trains the allowlist up, and per-project secret grants.
  /api/security — the persisted, acknowledgeable security-alert store.
Both expose an SSE `/stream` fed from the in-process bus, mirroring the Runs tab.
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import egress, egress_auto, lanaccess, secctx, security, sse
from .auth import require_user
from .db import get_db

router = APIRouter(prefix="/api/egress", tags=["egress"],
                   dependencies=[Depends(require_user)])
security_router = APIRouter(prefix="/api/security", tags=["security"],
                            dependencies=[Depends(require_user)])


def channel_feed(channel: str):
    """The live feed for one channel, shared by its own /stream endpoint and
    the multiplexed /api/events (backend/sse.py)."""
    return sse.channel_subscription(
        channel, [{"type": "stream_open", "channel": channel}])


async def _channel_stream(channel: str):
    return sse.sse_response(channel_feed(channel))


# --- egress: live feed -------------------------------------------------------

@router.get("/events")
async def recent_events(limit: int = 200, project: str | None = None):
    db = await get_db()
    try:
        q = ("SELECT id, project_slug, host, method, path, bytes_out, bytes_in, verdict, "
             "reason, created_at, peer_ip, peer_port, box_id, service_id FROM egress_events")
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


class ApproveBody(BaseModel):
    # the project an UNATTRIBUTED (shared-box, no turn) row belongs to: an
    # approval always writes a project's own list, so the operator names one
    project: str | None = None
    # allow for an hour only, without writing any list (egress.approve_host_once)
    once: bool = False


@router.post("/pending/{pid}/approve")
async def approve(pid: int, body: ApproveBody | None = None):
    db = await get_db()
    try:
        proj = body.project if body else None
        if body and body.once:
            res = await egress.approve_host_once(db, pid, project=proj)
        else:
            res = await egress.approve_host(db, pid, project=proj)
    finally:
        await db.close()
    if not res.get("ok") and res.get("needs_project"):
        raise HTTPException(status_code=409, detail="needs_project")
    if not res.get("ok") and "cannot be allowed" in res.get("error", ""):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


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
    last 24h, and hosts waiting on the operator now.

    A host you (or the reviewer) approved later is allowed, and the deny that
    queued it stops counting as blocked: the counts have to add up to what the
    decision log beside them shows. A `cut` is never superseded."""
    db = await get_db()
    try:
        where, args = "e.created_at > datetime('now', '-1 day')", ()
        if project:
            where += " AND e.project_slug = ?"
            args = (project,)
        async with db.execute(
                "SELECT COUNT(DISTINCT CASE WHEN e.verdict IN "
                "('allow','auto_allow','approved','reviewer_approved') THEN e.host END) "
                "AS allowed, "
                "COUNT(DISTINCT CASE WHEN e.verdict IN ('deny','auto_deny','cut') "
                "AND (e.verdict = 'cut' OR NOT EXISTS ("
                "SELECT 1 FROM egress_events a WHERE a.host = e.host AND a.id > e.id "
                "AND a.project_slug IS e.project_slug "
                "AND a.verdict IN ('approved','reviewer_approved'))) "
                f"THEN e.host END) AS denied FROM egress_events e WHERE {where}", args) as cur:
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
    # the list's own key: a project slug, 'profile:<id>' for a profile's list,
    # or '__general__' for the Default profile's (the old shared list)
    project: str = ""
    host: str = ""
    id: int | None = None          # an auto entry's id (source='auto')
    list: str = "allow"            # allow | deny


@router.post("/allowlist/revoke")
async def revoke(body: RevokeBody):
    db = await get_db()
    try:
        if body.id is not None:
            res = await egress.revoke_auto(db, body.id)
        else:
            res = await egress.remove_host(db, body.project, body.host,
                                           which="deny" if body.list == "deny" else "allow")
    finally:
        await db.close()
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res["error"])
    return res


@router.post("/auto/{aid}/promote")
async def promote_auto(aid: int):
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
    if not res.get("ok") and res.get("needs_project"):
        # same answer as the pending-approve route: the caller names a project
        raise HTTPException(status_code=409, detail="needs_project")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


# --- egress: per-project policy ---------------------------------------------

class PolicyBody(BaseModel):
    # the project's OWN lists (DESIGN-BOXES (c)); either may be omitted to
    # leave it as it is. The profile is changed with PUT /api/projects/{slug}/profile.
    allow: list[str] | None = None
    deny: list[str] | None = None
    # pre-profiles shape, still accepted while clients move over: the mode
    # picks the profile that reproduces it (egress.set_policy)
    mode: str | None = None
    inherit_general: bool = True
    hosts: list[str] | None = None


@router.get("/policy/{slug}")
async def get_policy(slug: str):
    """{profile:{id,name,default}, project_allow, project_deny, effective_allow,
    effective_deny} (+ the pre-profiles keys, read-only)."""
    db = await get_db()
    try:
        return await egress.get_policy(db, slug)
    finally:
        await db.close()


@router.put("/policy/{slug}")
async def put_policy(slug: str, body: PolicyBody):
    db = await get_db()
    try:
        if body.allow is None and body.deny is None and body.mode is not None:
            res = await egress.set_policy(db, slug, mode=body.mode,
                                          inherit_general=body.inherit_general,
                                          hosts=body.hosts or [])
        else:
            res = await egress.set_lists(db, slug, allow=body.allow, deny=body.deny)
    finally:
        await db.close()
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class PromoteBody(BaseModel):
    host: str
    profile_id: int | None = None  # None = the project's own profile
    list: str = "allow"            # allow | deny


@router.post("/policy/{slug}/promote")
async def promote_to_profile(slug: str, body: PromoteBody, user: dict = Depends(require_user)):
    """Move a host from the project's own list onto a profile's (one call; a
    `profile_changed` event)."""
    db = await get_db()
    try:
        res = await egress.promote_to_profile(
            db, slug, body.host, body.profile_id,
            which="deny" if body.list == "deny" else "allow",
            actor=str(user.get("username") or "operator"))
    finally:
        await db.close()
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


# --- egress: per-project LAN access (backend/lanaccess.py) -----------------

class LanBody(BaseModel):
    # either may be omitted to leave it as it is
    enabled: bool | None = None
    allow: list[str] | None = None


@router.get("/lan/{slug}")
async def get_lan(slug: str):
    """{enabled, allow, host_ips}: OFF by default. host_ips are the addresses
    that are never reachable (the Jav3 host), shown so a CIDR that covers the
    host reads as carved out."""
    db = await get_db()
    try:
        cfg = await lanaccess.get(db, slug)
    finally:
        await db.close()
    return {"slug": slug, **cfg, "host_ips": sorted(lanaccess.host_ips())}


@router.put("/lan/{slug}")
async def put_lan(slug: str, body: LanBody, user: dict = Depends(require_user)):
    db = await get_db()
    try:
        res = await lanaccess.set_(db, slug, enabled=body.enabled, allow=body.allow,
                                   actor=str(user.get("username") or "operator"))
    finally:
        await db.close()
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


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


class BaselineBody(BaseModel):
    scope: str = "program"          # program: this exe in this unit | unit: everything in the unit


@security_router.post("/events/{eid}/baseline")
async def allow_process(eid: int, body: BaselineBody,
                        user: dict = Depends(require_user)):
    """Stop an unexpected_process alert from coming back: add its program (or
    its whole systemd unit) to the operator's baseline, and acknowledge every
    waiting alert that entry now covers. Audited as a record-tier event."""
    from .vm import procview
    if body.scope not in ("program", "unit"):
        raise HTTPException(status_code=400, detail="scope must be program or unit")
    db = await get_db()
    try:
        ev = await security.get_event(db, eid)
        if ev is None:
            raise HTTPException(status_code=404, detail="no such event")
        if ev["kind"] != "unexpected_process":
            raise HTTPException(status_code=400,
                                detail="only an unexpected-process alert can be allowed")
        d = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
        exe, unit = str(d.get("exe") or ""), str(d.get("unit") or "")
        try:
            entry = await procview.add_operator_entry(
                db, "*" if body.scope == "unit" else exe, unit,
                by=str(user.get("username") or "operator"))
        except procview.BaselineError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # every waiting alert this entry now covers (this one included)
        n = 0
        async with db.execute("SELECT id, detail FROM security_events "
                              "WHERE kind = 'unexpected_process' AND acknowledged = 0") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            try:
                rd = json.loads(r["detail"] or "{}")
            except ValueError:
                continue
            if not isinstance(rd, dict) or str(rd.get("unit") or "") != unit:
                continue
            if body.scope == "program" and str(rd.get("exe") or "") != exe:
                continue
            await security.acknowledge(db, r["id"])
            n += 1
        what = f"everything in {unit}" if body.scope == "unit" else f"{exe} in {unit or 'no unit'}"
        # an audit line for a change you just made: recorded, nothing to do
        audit = await security.raise_event(
            db, kind="proc_baseline_changed", severity="info",
            summary=f"You allowed {what} in every box's process baseline",
            detail={"scope": body.scope, "exe": entry["exe"], "unit": unit,
                    "from_event": eid, "acknowledged": n})
        await security.acknowledge(db, audit)
        return {"ok": True, **entry, "acknowledged": n}
    finally:
        await db.close()


@security_router.get("/baseline")
async def operator_baseline():
    """What the operator allowed from alerts, newest last: [{exe, unit, by, at}].
    exe "*" is a whole unit."""
    from .vm import procview
    db = await get_db()
    try:
        return {"entries": await procview.operator_entries(db)}
    finally:
        await db.close()


class BaselineRemoveBody(BaseModel):
    exe: str
    unit: str = ""


@security_router.post("/baseline/remove")
async def remove_operator_baseline(body: BaselineRemoveBody):
    """Take one allowance back off: the process alerts again from its next boot."""
    from .vm import procview
    db = await get_db()
    try:
        if not await procview.remove_operator_entry(db, body.exe, body.unit):
            raise HTTPException(status_code=404, detail="that entry is not on the list")
        what = f"everything in {body.unit}" if body.exe == "*" else f"{body.exe} in {body.unit or 'no unit'}"
        audit = await security.raise_event(
            db, kind="proc_baseline_changed", severity="info",
            summary=f"You took {what} off the allowed-process list",
            detail={"scope": "remove", "exe": body.exe, "unit": body.unit})
        await security.acknowledge(db, audit)
        return {"ok": True}
    finally:
        await db.close()


@security_router.post("/events/ack_all")
async def ack_all(only: str | None = None, exclude: str | None = None):
    db = await get_db()
    try:
        return await security.acknowledge_all(db, only=only, exclude=exclude)
    finally:
        await db.close()


@security_router.get("/stream")
async def security_stream():
    return await _channel_stream(security.SECURITY_CHAN)
