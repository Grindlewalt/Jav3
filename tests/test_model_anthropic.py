"""The Anthropic Messages adapter, against a recorded-shape SSE stream served by
an httpx MockTransport: request translation (system, tools, blocks, headers),
stream -> the gateway's token/message events, usage normalisation, verbatim
replay of thinking blocks, and the strip-and-retry recovery. Offline."""
import json

import httpx
import pytest

from backend import providers
from backend.agent import adapters, budget as budget_mod
from backend.agent.budget import Budget
from backend.agent.model import ModelGateway
from backend.db import init_db

KEY = "sk-ant-test-0123456789"
MODEL = "anthropic/claude-sonnet-5"
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]}}}]


def sse(*events) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
                   for e in events).encode()


STREAM = sse(
    {"type": "message_start", "message": {"id": "msg_1", "usage": {
        "input_tokens": 100, "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 0, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0,
     "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "thinking_delta", "thinking": "plan"}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "signature_delta", "signature": "SIG"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1,
     "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "text_delta", "text": "Hel"}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "text_delta", "text": "lo"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {
        "type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}},
    {"type": "content_block_delta", "index": 2,
     "delta": {"type": "input_json_delta", "partial_json": "{\"path\": "}},
    {"type": "content_block_delta", "index": 2,
     "delta": {"type": "input_json_delta", "partial_json": "\"a.md\"}"}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
     "usage": {"output_tokens": 20}},
    {"type": "message_stop"},
)


@pytest.fixture
async def anthropic(tmp_env, monkeypatch):
    await init_db()
    providers.set_key("anthropic", KEY)
    seen: list[httpx.Request] = []
    replies: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if replies:
            return replies.pop(0)
        return httpx.Response(200, content=STREAM,
                              headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(adapters, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    return seen, replies


async def _drain(gen):
    return [ev async for ev in gen]


async def test_request_shape_and_stream(anthropic):
    seen, _ = anthropic
    msgs = [{"role": "system", "content": "be brief"},
            {"role": "user", "content": "read a.md"}]
    out = await _drain(ModelGateway().complete(msgs, tools=TOOLS, model_name=MODEL))

    req = seen[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == KEY
    assert req.headers["anthropic-version"] == providers.ANTHROPIC_VERSION
    assert "authorization" not in req.headers
    body = json.loads(req.content)
    assert body["model"] == "claude-sonnet-5" and body["stream"] is True
    assert body["system"] == "be brief"
    assert body["messages"] == [{"role": "user",
                                 "content": [{"type": "text", "text": "read a.md"}]}]
    assert body["tools"] == [{"name": "read_file", "description": "Read a file",
                              "input_schema": TOOLS[0]["function"]["parameters"]}]
    assert body["max_tokens"] == 128000
    assert "temperature" not in body        # the current Claude line rejects it
    assert body["cache_control"] == {"type": "ephemeral"}

    assert [e["text"] for e in out if e["type"] == "token"] == ["Hel", "lo"]
    final = out[-1]
    assert final["type"] == "message" and final["content"] == "Hello"
    assert final["tool_calls"] == [{"id": "toolu_1", "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": "a.md"})}}]
    assert final["usage"] == {"prompt_tokens": 1000, "completion_tokens": 20,
                              "prompt_cache_hit_tokens": 900,
                              "prompt_cache_miss_tokens": 100}
    blocks = final["provider_blocks"]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": "plan", "signature": "SIG"}
    assert blocks[2]["input"] == {"path": "a.md"} and "_json" not in blocks[2]


async def test_replay_is_verbatim_and_tool_results_merge(anthropic):
    seen, _ = anthropic
    first = (await _drain(ModelGateway().complete(
        [{"role": "user", "content": "go"}], tools=TOOLS, model_name=MODEL)))[-1]
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": first["content"],
         "tool_calls": first["tool_calls"], "provider_blocks": first["provider_blocks"]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "file body"},
        {"role": "user", "content": "(inbox) hello"},
    ]
    await _drain(ModelGateway().complete(history, tools=TOOLS, model_name=MODEL))
    msgs = json.loads(seen[1].content)["messages"]
    assert msgs[1] == {"role": "assistant", "content": first["provider_blocks"]["content"]}
    # the tool result and the following user text are ONE user turn
    assert msgs[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file body"},
        {"type": "text", "text": "(inbox) hello"}]}


async def test_rejected_thinking_blocks_are_stripped_and_retried(anthropic):
    seen, replies = anthropic
    replies.append(httpx.Response(400, json={"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "messages.1.content.0: Invalid `signature` in `thinking` block."}}))
    history = [{"role": "user", "content": "go"},
               {"role": "assistant", "content": "", "tool_calls": [],
                "provider_blocks": {"kind": "anthropic", "content": [
                    {"type": "thinking", "thinking": "x", "signature": "OLD"},
                    {"type": "text", "text": "ok"}]}},
               {"role": "user", "content": "more"}]
    out = await _drain(ModelGateway().complete(history, model_name=MODEL))
    assert out[-1]["content"] == "Hello"
    retried = json.loads(seen[1].content)["messages"][1]["content"]
    assert retried == [{"type": "text", "text": "ok"}]


async def test_budget_uses_the_models_cache_price(anthropic):
    b = Budget(max_input=10**9, max_output=10**9)
    token = budget_mod.active_budget.set(b)
    try:
        await _drain(ModelGateway().complete(
            [{"role": "user", "content": "x"}], model_name=MODEL))
    finally:
        budget_mod.active_budget.reset(token)
    info = providers.model_info("anthropic", "claude-sonnet-5")
    assert b.cache_hit == 900
    assert b.charged_input == pytest.approx(
        100 + 900 * info["price_cache"] / info["price_in"])


async def test_error_status_raises_model_error(anthropic):
    from backend.agent.model import ModelError
    _, replies = anthropic
    replies.append(httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}))
    with pytest.raises(ModelError) as ei:
        await _drain(ModelGateway().complete(
            [{"role": "user", "content": "x"}], model_name=MODEL))
    assert ei.value.status == 401 and KEY not in str(ei.value)


def test_translation_images_and_system():
    system, msgs = adapters.to_anthropic([
        {"role": "system", "content": "a"},
        {"role": "system", "content": "b"},
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "t", "arguments": "not json"}}]},
    ])
    assert system == "a\n\nb"
    assert msgs[0]["content"][1] == {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "QUJD"}}
    assert msgs[1]["content"] == [{"type": "tool_use", "id": "c1", "name": "t",
                                   "input": {"_raw": "not json"}}]
