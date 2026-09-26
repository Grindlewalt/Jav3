"""Long-lived SSE feeds, and the one connection that carries all of them.

Every open Jav3 tab used to hold its own GET for each live feed: the GUI
control channel, security alerts, agent-run notices, and on the Network page
the egress feed. Over plain http a browser allows six connections per host,
shared by EVERY tab — so two tabs were enough to use them all up and every
ordinary fetch after that queued forever (the Work page's "+ window" never
opened: its layout GET never got a socket).

So a feed is now a Subscription (a bus queue, the events it opens with, and
how to let go of it), and there are two ways to read one:

  sse_response(sub)          the old per-feed endpoint, byte-for-byte
  multiplex({topic: sub})    /api/events: many feeds on one connection, each
                             event tagged with its topic (backend/events_api.py)

Both share the same subscription code, so a feed's semantics (what it sends
first, what it publishes, when it lets go) cannot drift between the two.
"""
import asyncio
import json
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from fastapi.responses import StreamingResponse

from . import bus

KEEPALIVE_S = 25


def sse(d: dict) -> str:
    return f"data: {json.dumps(d)}\n\n"


@dataclass
class Subscription:
    """One live feed: where its events arrive, what it says on connect, and
    the cleanup to run exactly once when the reader goes away."""
    queue: asyncio.Queue
    first: list[dict] = field(default_factory=list)
    on_close: Callable[[], None] | None = None
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.on_close:
            self.on_close()


def channel_subscription(channel: str, first: list[dict]) -> Subscription:
    """A plain bus channel (security, egress, agent notices)."""
    q = bus.subscribe(channel)
    return Subscription(q, first, lambda: bus.unsubscribe(channel, q))


async def feed(sub: Subscription) -> AsyncIterator[dict | None]:
    """One feed's events in order; None when it has been quiet for
    KEEPALIVE_S (the caller turns that into a keepalive comment)."""
    try:
        for ev in sub.first:
            yield ev
        while True:
            try:
                yield await asyncio.wait_for(sub.queue.get(), timeout=KEEPALIVE_S)
            except asyncio.TimeoutError:
                yield None
    finally:
        sub.close()


async def multiplex(subs: dict[str, Subscription]) -> AsyncIterator[tuple[str, dict] | None]:
    """Several feeds merged: (topic, event), or None as the ONE keepalive for
    the whole connection. Order is preserved within a topic (one pump per
    feed, one merged FIFO); across topics it is arrival order. Every
    subscription is released however this ends — disconnect, cancel, aclose."""
    # bounded so a slow client pushes back onto the per-feed bus queues, which
    # already shed their oldest events rather than grow (bus.publish_to)
    merged: asyncio.Queue = asyncio.Queue(maxsize=1000)
    tasks: list[asyncio.Task] = []

    async def pump(topic: str, q: asyncio.Queue):
        while True:
            ev = await q.get()
            await merged.put((topic, ev))

    try:
        for topic, sub in subs.items():
            for ev in sub.first:
                yield topic, ev
        tasks = [asyncio.create_task(pump(t, s.queue)) for t, s in subs.items()]
        while True:
            try:
                yield await asyncio.wait_for(merged.get(), timeout=KEEPALIVE_S)
            except asyncio.TimeoutError:
                yield None
    finally:
        for t in tasks:
            t.cancel()
        for sub in subs.values():
            sub.close()


def stream_response(agen: AsyncIterator, render: Callable) -> StreamingResponse:
    async def gen():
        try:
            async for item in agen:
                yield ": keepalive\n\n" if item is None else render(item)
        except asyncio.CancelledError:
            pass
        finally:
            await agen.aclose()
    return StreamingResponse(gen(), media_type="text/event-stream")


def sse_response(sub: Subscription) -> StreamingResponse:
    """A single feed as its own SSE endpoint (the pre-/api/events shape)."""
    return stream_response(feed(sub), sse)
