"""GET /api/events — every long-lived feed on ONE connection.

A browser allows six HTTP/1.1 connections per host, shared by all its tabs, and
each Jav3 tab used to hold three or four feeds open for its whole life. Two
tabs over plain http and every ordinary fetch queued forever. The SPA now opens
this endpoint once per browser (a leader tab, frontend/src/events.js) and fans
the events out to its tabs over a BroadcastChannel.

    GET /api/events?topics=gui,security,notices,egress,procs,runs   (default: all)

Each event is one SSE `data:` line:

    {"topic": "security", "event": {...the feed's own payload...}}

plus `"to": "<tab id>"` on a GUI event addressed to one tab (backend/gui.py
push(tab=...)); the browser delivers that one only in that tab. Each topic
opens with the same events its own endpoint sends first (`stream_open`), keeps
its order, and there is one keepalive for the whole connection. The per-feed
endpoints (/api/gui/stream, /api/security/stream, /api/agents/notices/stream,
/api/egress/stream) still work and share the same subscription code.

Authorisation is per topic: each topic names the dependency its own endpoint
requires, and a request naming a topic the caller may not read is refused
outright rather than silently narrowed.
"""
import re
from typing import Callable

from fastapi import APIRouter, HTTPException, Request

from . import agents_run, bus, egress, gui, procview_api, security, sse
from .auth import require_user
from .egress_api import channel_feed

router = APIRouter(prefix="/api", tags=["events"])


# A job's own bus channel is its job_id, a fresh uuid4 hex per run (research,
# funnel, plan, deploy_agents); no other channel looks like one.
_JOB_CHAN = re.compile(r"[0-9a-f]{32}")
# what a live job tree reads (JobTree.jsx, PlanPanel.jsx). Not `token`: every
# leaf's streamed reply would go to every browser for a tree that ignores it.
RUN_EVENTS = frozenset({"job_start", "node_spawned", "node_status", "tool",
                        "node_done", "error", "job_final", "plan_item"})


def _pick_run_event(channel: str, ev: dict) -> dict | None:
    if not isinstance(ev, dict) or ev.get("type") not in RUN_EVENTS:
        return None
    if not _JOB_CHAN.fullmatch(channel):
        return None
    return {**ev, "job_id": channel}


def runs_feed() -> sse.Subscription:
    """Every agent job's tree events, each stamped with its job_id. Replaces
    the per-run GET /api/runs/{cid}/stream a JobTree used to hold open: the
    browser takes the snapshot from GET /api/runs/{cid}/tree?depth=full and
    follows the live events here, filtered to its own job."""
    q = bus.tap(_pick_run_event)
    return sse.Subscription(q, [{"type": "stream_open", "channel": "runs"}],
                            lambda: bus.untap(q))


# topic -> (auth check, the same as the feed's own endpoint; subscription factory)
TOPICS: dict[str, tuple[Callable[[Request], dict], Callable[[], sse.Subscription]]] = {
    "gui": (require_user, gui.open_mux),
    "security": (require_user, lambda: channel_feed(security.SECURITY_CHAN)),
    "notices": (require_user, agents_run.notice_feed),
    "egress": (require_user, lambda: channel_feed(egress.EGRESS_CHAN)),
    "procs": (require_user, procview_api.procs_feed),   # WP4: Security > Persistent
    # WP5: image builder progress ({"type":"image_build", phase, variant, ...})
    "vm-images": (require_user, lambda: channel_feed("vm-images")),
    # WP1: {"type":"box_up"|"box_down","box":Box.to_json()} (VM manager refresh)
    "vm-boxes": (require_user, lambda: channel_feed("vm-boxes")),
    # WP3: {"type":"service_changed"|"service_state","service_id",...}
    # (vm/services.py): the service lists refetch on either
    "services": (require_user, lambda: channel_feed("services")),
    # agent-job trees ({..., "job_id"}): JobTree / PlanPanel filter by job
    "runs": (require_user, runs_feed),
}


def _render(item: tuple[str, dict]) -> str:
    topic, ev = item
    out = {"topic": topic}
    if isinstance(ev, dict) and "_to_tab" in ev:
        ev = dict(ev)
        out["to"] = ev.pop("_to_tab")
    out["event"] = ev
    return sse.sse(out)


@router.get("/events")
async def events(request: Request, topics: str = ""):
    wanted = [t.strip() for t in topics.split(",") if t.strip()] or list(TOPICS)
    unknown = [t for t in wanted if t not in TOPICS]
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"unknown topic(s): {', '.join(unknown)}")
    wanted = list(dict.fromkeys(wanted))
    for t in wanted:
        check, _ = TOPICS[t]
        try:
            check(request)
        except HTTPException as e:
            raise HTTPException(status_code=e.status_code,
                                detail=f"{t}: {e.detail}") from None
    # every check passed before anything subscribes, so a refusal leaks nothing
    subs = {t: TOPICS[t][1]() for t in wanted}
    return sse.stream_response(sse.multiplex(subs), _render)
