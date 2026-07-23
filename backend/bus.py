"""In-process event bus for live agent-job streaming.

An agent job (M7) runs as a background asyncio task that publishes lifecycle
and activity events; an SSE endpoint subscribes to the job's id and streams
them to the browser. Both live in the same event loop and process, so a plain
dict of asyncio.Queues is all this needs — no Redis, no cross-process concern.

Publishing never touches the DB and never raises into the orchestrator: a slow
or gone subscriber must not stall or crash the job.
"""
import asyncio

_subscribers: dict[str, set[asyncio.Queue]] = {}

# terminal sentinel so a subscriber's drain loop knows the job is finished
JOB_END = {"type": "job_end"}


def subscribe(job_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.setdefault(job_id, set()).add(q)
    return q


def unsubscribe(job_id: str, q: asyncio.Queue) -> None:
    subs = _subscribers.get(job_id)
    if subs:
        subs.discard(q)
        if not subs:
            _subscribers.pop(job_id, None)


def subscriber_count(channel: str) -> int:
    """How many live subscriptions a channel has (e.g. open GUI tabs), so a
    publisher can report honestly whether anyone was listening."""
    return len(_subscribers.get(channel, ()))


def publish(job_id: str, event: dict) -> None:
    for q in list(_subscribers.get(job_id, ())):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # protect against a slow browser: drop the oldest event and retry.
            # Token events are the firehose, so this mostly sheds those.
            try:
                q.get_nowait()
                q.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass


def close_job(job_id: str) -> None:
    """Signal end-of-job to every subscriber; they unsubscribe themselves."""
    publish(job_id, JOB_END)


def announce_job(job_id: str, root_id: int, title: str) -> None:
    """Tell the chat turn that launched this job (if any) that it started, so
    the GUI mounts a live JobTree inline. No-op outside a chat turn."""
    from . import runtime
    chan = runtime.event_chan.get()
    if chan:
        publish(chan, {"type": "job", "job_id": job_id, "root_id": root_id,
                       "title": title})
