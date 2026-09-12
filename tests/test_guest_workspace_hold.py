"""The workspace-push token must survive an operator interrupt without leaking.

A top-level chat turn calls acquire_workspace(slug) (first-in pushes the project
copy into the guest); guest_turn's `finally` releases it. If the turn is
cancelled mid-stream and the async generator is never closed, that release never
runs, the token leaks, and the NEXT top-level turn's acquire_workspace sees a
phantom concurrent holder -> it skips the push -> the guest shows an EMPTY
project -> the agent thinks its work was wiped and rebuilds from scratch. So the
consumer (chat.py) must close the generator; these tests lock that contract.
"""
import asyncio
import json

import pytest

from backend.vm import guest_turn as gt
from backend.vm import lifecycle


def test_acquire_release_accounting():
    slug = "acct-test"
    gt._ws_holds.pop(slug, None)
    try:
        assert gt.acquire_workspace(slug) is True      # first-in owns the push
        assert gt.acquire_workspace(slug) is False     # a concurrent joiner does not
        assert gt.release_workspace(slug) is False     # joiner leaves; owner still in
        assert gt.release_workspace(slug) is True      # owner leaves last -> sweep
        assert slug not in gt._ws_holds                # fully balanced, no residue
    finally:
        gt._ws_holds.pop(slug, None)


def _mock_transport(monkeypatch, recv_seq):
    """Drive guest_turn's vsock transport from a list of recv() results
    (bytes, or an Event to block on forever)."""
    monkeypatch.setattr(gt.workspace_xfer, "build_merged_tar", lambda s: b"")

    async def _acquire():
        return None
    monkeypatch.setattr(lifecycle.vm, "acquire", _acquire)

    class FakeSock:
        def connect(self, *a):
            pass

        def setblocking(self, *a):
            pass

        def close(self):
            pass
    monkeypatch.setattr(gt.socket, "socket", lambda *a, **k: FakeSock())

    loop = asyncio.get_running_loop()

    async def _run_in_executor(_ex, fn, *a):
        return fn(*a)
    monkeypatch.setattr(loop, "run_in_executor", _run_in_executor)

    async def _sendall(_sock, _data):
        return None
    monkeypatch.setattr(loop, "sock_sendall", _sendall)

    it = iter(recv_seq)

    async def _recv(_sock, _n):
        try:
            nxt = next(it)
        except StopIteration:
            return b""                       # stream closed -> generator returns
        if isinstance(nxt, asyncio.Event):
            await nxt.wait()                 # block "mid-turn" until cancelled
            return b""
        return nxt
    monkeypatch.setattr(loop, "sock_recv", _recv)


async def test_hold_released_when_generator_closed_mid_turn(monkeypatch):
    slug = "interrupt-test"
    gt._ws_holds.pop(slug, None)
    token = json.dumps({"type": "token", "content": "hi"}).encode() + b"\n"
    _mock_transport(monkeypatch, [token, asyncio.Event()])  # one event, then block
    try:
        agen = gt.guest_turn(1, "sys", [], active_slug=slug, push_workspace=True)
        ev = await agen.__anext__()
        assert ev["type"] == "token"
        assert gt._ws_holds.get(slug) == 1        # token acquired, push happened

        # the operator hits stop: the task is cancelled and the consumer closes
        # the suspended generator. Its finally must run and free the token.
        await agen.aclose()
        assert slug not in gt._ws_holds

        # ...and because it's freed, the NEXT top-level turn owns the push again
        assert gt.acquire_workspace(slug) is True
    finally:
        gt._ws_holds.pop(slug, None)


async def test_hold_freed_across_a_real_cancel(monkeypatch):
    """End-to-end shape: consume in a task, cancel it the way an interrupt does,
    and (closing the generator) confirm the token is gone so the next turn pushes."""
    slug = "cancel-test"
    gt._ws_holds.pop(slug, None)
    token = json.dumps({"type": "token", "content": "hi"}).encode() + b"\n"
    _mock_transport(monkeypatch, [token, asyncio.Event()])
    try:
        agen = gt.guest_turn(1, "sys", [], active_slug=slug, push_workspace=True)

        async def consume():
            async for _ in agen:            # will block on the second recv
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)           # let it stream the first event + block
        assert gt._ws_holds.get(slug) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # the consumer's own `finally` (mirrored here) closes the generator
        await agen.aclose()
        assert slug not in gt._ws_holds
    finally:
        gt._ws_holds.pop(slug, None)
