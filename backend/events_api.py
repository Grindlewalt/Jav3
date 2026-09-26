"""GET /api/events — every long-lived feed on ONE connection.

A browser allows six HTTP/1.1 connections per host, shared by all its tabs, and
each Jav3 tab used to hold three or four feeds open for its whole life. Two
tabs over plain http and every ordinary fetch queued forever. The SPA now opens
this endpoint once per browser (a leader tab, frontend/src/events.js) and fans
the events out to its tabs over a BroadcastChannel.

    GET /api/events?topics=gui,security,notices,egress     (default: all)

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
from typing import Callable

from fastapi import APIRouter, HTTPException, Request

from . import agents_run, egress, gui, security, sse
from .auth import require_user
from .egress_api import channel_feed

router = APIRouter(prefix="/api", tags=["events"])


# topic -> (auth check, the same as the feed's own endpoint; subscription factory)
TOPICS: dict[str, tuple[Callable[[Request], dict], Callable[[], sse.Subscription]]] = {
    "gui": (require_user, gui.open_mux),
    "security": (require_user, lambda: channel_feed(security.SECURITY_CHAN)),
    "notices": (require_user, agents_run.notice_feed),
    "egress": (require_user, lambda: channel_feed(egress.EGRESS_CHAN)),
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
