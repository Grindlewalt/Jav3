"""GET /api/vm/processes — the Security > Persistent view (WP4).

Shape: docs/boxes-contract.md J(b). The data is the poller's latest per-box
row (backend/vm/procview.py); live updates ride the `procs` topic of the one
multiplexed /api/events stream (backend/events_api.py), never a URL of their
own. Every string in a row came from a guest and is untrusted: cleaned and
clipped by procview, and rendered as text by the clients.
"""
from fastapi import APIRouter, Depends, HTTPException

from . import sse
from .auth import require_user
from .vm import boxes, procview

router = APIRouter(prefix="/api/vm", tags=["vm"], dependencies=[Depends(require_user)])


@router.get("/processes")
async def processes(box: str | None = None):
    if not boxes.enabled():
        return {"enabled": False, "boxes": []}
    if box is not None and boxes.get(box) is None:
        raise HTTPException(status_code=404, detail="unknown box")
    procview.ensure_started()
    return {"enabled": True, "boxes": procview.rows(box)}


def procs_feed() -> sse.Subscription:
    """The `procs` topic: opens with the current row of every box (a large
    one is announced so the client GETs it), then each poll's rows."""
    first: list[dict] = [{"type": "stream_open", "channel": procview.PROCS_CHAN}]
    if boxes.enabled():
        procview.ensure_started()
        first += [procview.event_for(r) for r in procview.rows()]
    return sse.channel_subscription(procview.PROCS_CHAN, first)
