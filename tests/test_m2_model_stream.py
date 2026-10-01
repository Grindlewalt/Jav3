"""M2 (2026-09-30): the OpenAI-wire stream reader no longer takes a cut-off
stream for an answer (ROBUST-14), a dropped stream is retried (ROBUST-15), and a
call that was abandoned or dropped is still ledgered (ROBUST-23)."""
import asyncio
import json

import httpx
import pytest

from backend.agent import adapters, budget as budget_mod
from backend.agent.model import ModelClient, ModelError, ModelGateway
from backend.config import settings
from backend.db import get_db, init_db


def _chunk(delta=None, finish=None, usage=None):
    obj = {"choices": [{"delta": delta or {}, "finish_reason": finish}]} if (
        delta is not None or finish) else {"choices": []}
    if usage:
        obj["usage"] = usage
    return "data: " + json.dumps(obj) + "\n\n"


DONE = "data: [DONE]\n\n"


class _Body(httpx.AsyncByteStream):
    """A response body that yields its chunks, then optionally drops."""
    def __init__(self, chunks, then=None):
        self.chunks, self.then = chunks, then

    async def __aiter__(self):
        for c in self.chunks:
            yield c.encode()
        if self.then is not None:
            raise self.then


def _resp(chunks, then=None):
    return httpx.Response(200, stream=_Body(chunks, then),
                          headers={"content-type": "text/event-stream"})


@pytest.fixture
def wire(monkeypatch):
    """Scripted responses, one per HTTP attempt; no backoff sleeps."""
    monkeypatch.setattr(settings, "model_retries", 2)
    monkeypatch.setattr(settings, "model_retry_backoff_seconds", 0)
    replies: list = []
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return replies.pop(0)

    monkeypatch.setattr(adapters, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    return replies, calls


async def _run(client=None, **kw):
    out = []
    async for ev in (client or ModelClient(api_key="k")).complete(
            [{"role": "user", "content": "x"}], **kw):
        out.append(ev)
    return out


# --- ROBUST-14 -----------------------------------------------------------------

TOOL_PARTIAL = _chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {
    "name": "write_file", "arguments": '{"path": "a.txt", "content": "half'}}]})


async def test_stream_that_ends_without_done_or_finish_is_an_error(wire):
    replies, calls = wire
    replies += [_resp([TOOL_PARTIAL]) for _ in range(3)]   # every attempt is cut off
    with pytest.raises(ModelError, match="ended"):
        await _run()
    assert calls["n"] == 3                          # retried, then gave up


async def test_cut_off_stream_is_retried_and_the_whole_answer_wins(wire):
    replies, calls = wire
    replies += [_resp([_chunk({"content": "Step 1"})]),
                _resp([_chunk({"content": "Step 1 done."}),
                       _chunk({}, finish="stop"), DONE])]
    out = await _run()
    assert out[-1]["content"] == "Step 1 done."
    assert calls["n"] == 2


async def test_length_finish_is_surfaced_on_the_message(wire):
    replies, _ = wire
    replies.append(_resp([_chunk({"content": "Step 1 done. Step 2 is"}),
                          _chunk({}, finish="length"), DONE]))
    out = await _run()
    assert out[-1]["content"] == "Step 1 done. Step 2 is"
    assert out[-1]["finish_reason"] == "length"


async def test_normal_stop_carries_no_truncation_and_done_alone_is_fine(wire):
    replies, _ = wire
    replies.append(_resp([_chunk({"content": "hi"}), _chunk({}, finish="stop"),
                          _chunk(usage={"prompt_tokens": 5, "completion_tokens": 1}), DONE]))
    replies.append(_resp([_chunk({"content": "hi"}), DONE]))   # a server with no finish_reason
    for _i in range(2):
        out = await _run()
        assert out[-1]["content"] == "hi"
        assert out[-1].get("finish_reason") != "length"


async def test_error_object_in_the_stream_is_an_error(wire):
    replies, calls = wire
    err = 'data: {"error": {"message": "upstream exploded", "code": 503}}\n\n'
    replies += [_resp([_chunk({"content": "partial"}), err]) for _ in range(3)]
    with pytest.raises(ModelError, match="upstream exploded") as ei:
        await _run()
    assert ei.value.status == 503 and calls["n"] == 3     # 5xx: retried


async def test_error_object_with_a_4xx_code_is_not_retried(wire):
    replies, calls = wire
    err = 'data: {"error": {"message": "context too long", "code": 400}}\n\n'
    replies.append(_resp([err]))
    with pytest.raises(ModelError, match="context too long"):
        await _run()
    assert calls["n"] == 1


# --- ROBUST-15 -----------------------------------------------------------------

async def test_drop_after_the_first_token_is_retried_once_with_a_retry_event(wire):
    replies, calls = wire
    replies += [_resp([_chunk({"content": "Hel"})],
                      then=httpx.RemoteProtocolError("peer closed connection")),
                _resp([_chunk({"content": "Hello"}), _chunk({}, finish="stop"), DONE])]
    out = await _run()
    kinds = [e["type"] for e in out]
    assert kinds == ["token", "retry", "token", "message"]
    assert out[-1]["content"] == "Hello" and calls["n"] == 2


async def test_drop_that_keeps_dropping_ends_in_the_error_after_the_retries(wire):
    replies, calls = wire
    replies += [_resp([_chunk({"content": "x"})], then=httpx.ReadError("dropped"))
                for _ in range(3)]
    with pytest.raises(httpx.ReadError):
        await _run()
    assert calls["n"] == 3




# --- ROBUST-23 -----------------------------------------------------------------

async def _rows():
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT conversation_id, input_tokens, output_tokens FROM model_calls")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def test_an_abandoned_stream_is_ledgered_and_charged(tmp_env, wire):
    await init_db()
    replies, _ = wire
    replies.append(_resp([_chunk({"content": "some words"}),
                          _chunk({"content": " more"}), _chunk({}, finish="stop"), DONE]))
    b = budget_mod.Budget(max_input=10_000_000, max_output=10_000_000)
    budget_mod.register("op-m2", b)
    try:
        gen = ModelGateway(api_key="k").complete(
            [{"role": "user", "content": "q" * 400}], conversation_id=None, op_id="op-m2")
        first = await gen.__anext__()
        assert first["type"] == "token"
        await gen.aclose()                           # what a stop does
    finally:
        budget_mod.release("op-m2")
    assert b.input_tokens >= 100 and b.output_tokens >= 1
    rows = await _rows()
    assert len(rows) == 1 and rows[0]["input_tokens"] >= 100


async def _spend(op, **kw):
    """Run a gateway call under a registered budget; returns (budget, events, error)."""
    b = budget_mod.Budget(max_input=10_000_000, max_output=10_000_000)
    budget_mod.register(op, b)
    events, err = [], None
    try:
        async for ev in ModelGateway(api_key="k").complete(
                [{"role": "user", "content": "q" * 400}], op_id=op, **kw):
            events.append(ev)
    except Exception as e:  # noqa: BLE001
        err = e
    finally:
        budget_mod.release(op)
    return b, events, err


async def test_a_completed_call_is_ledgered_once_with_the_reported_usage(tmp_env, wire):
    await init_db()
    replies, _ = wire
    replies.append(_resp([_chunk({"content": "hi"}), _chunk({}, finish="stop"),
                          _chunk(usage={"prompt_tokens": 50, "completion_tokens": 2}), DONE]))
    b, _events, err = await _spend("op-m2-ok")
    assert err is None and b.input_tokens == 50
    assert [r["input_tokens"] for r in await _rows()] == [50]


async def test_a_stream_that_failed_after_tokens_is_ledgered_not_free(tmp_env, wire):
    await init_db()
    replies, _ = wire
    replies += [_resp([_chunk({"content": "x" * 40})], then=httpx.ReadError("dropped"))
                for _ in range(3)]
    b, _events, err = await _spend("op-m2-fail")
    assert isinstance(err, httpx.ReadError)
    assert b.input_tokens >= 100                    # three attempts, each billed a prompt
    assert len(await _rows()) == 3


async def test_a_call_with_no_token_that_failed_costs_nothing(tmp_env, wire):
    await init_db()
    replies, _ = wire
    replies += [httpx.Response(503, text="busy") for _ in range(3)]
    b, _events, err = await _spend("op-m2-503")
    assert isinstance(err, ModelError) and b.input_tokens == 0
    assert await _rows() == []


async def test_a_cancelled_call_is_ledgered(tmp_env, monkeypatch):
    await init_db()
    started = asyncio.Event()

    async def hang(self, base, key, payload):
        yield {"type": "token", "text": "words"}
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(ModelClient, "_stream_once", hang)
    b = budget_mod.Budget(max_input=10_000_000, max_output=10_000_000)
    budget_mod.register("op-m2-cancel", b)

    async def consume():
        async for _ in ModelGateway(api_key="k").complete(
                [{"role": "user", "content": "q" * 400}], op_id="op-m2-cancel"):
            pass

    t = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 5)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    budget_mod.release("op-m2-cancel")
    assert b.input_tokens >= 100 and len(await _rows()) == 1


