"""The Gemini generateContent adapter against a stubbed SSE stream: request
translation (systemInstruction, functionDeclarations, key header), stream ->
token/message events, usage, and verbatim replay of thought-signed parts."""
import json

import httpx
import pytest

from backend import providers
from backend.agent import adapters
from backend.agent.model import ModelGateway
from backend.db import init_db

KEY = "AIza-test-0123456789"
MODEL = "google/gemini-2.5-flash"
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "additionalProperties": False}}}]


def sse(*chunks) -> bytes:
    return "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks).encode()


STREAM = sse(
    {"candidates": [{"content": {"role": "model", "parts": [
        {"text": "thinking...", "thought": True}, {"text": "Hi "}]}}]},
    {"candidates": [{"content": {"role": "model", "parts": [
        {"text": "there"},
        {"functionCall": {"name": "read_file", "args": {"path": "a.md"}},
         "thoughtSignature": "SIG1"}]}, "finishReason": "STOP"}],
     "usageMetadata": {"promptTokenCount": 1000, "cachedContentTokenCount": 600,
                       "candidatesTokenCount": 30, "thoughtsTokenCount": 12}},
)


@pytest.fixture
async def gemini(tmp_env, monkeypatch):
    await init_db()
    providers.set_key("google", KEY)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=STREAM,
                              headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(adapters, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    return seen


async def _drain(gen):
    return [ev async for ev in gen]


async def test_request_and_stream(gemini):
    out = await _drain(ModelGateway().complete(
        [{"role": "system", "content": "be brief"},
         {"role": "user", "content": "read a.md"}],
        tools=TOOLS, model_name=MODEL, temperature=0.2))
    req = gemini[0]
    assert req.url.path == "/v1beta/models/gemini-2.5-flash:streamGenerateContent"
    assert req.url.params["alt"] == "sse"
    assert req.headers["x-goog-api-key"] == KEY
    assert "key" not in req.url.params           # the key never rides the URL
    body = json.loads(req.content)
    assert body["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "read a.md"}]}]
    decl = body["tools"][0]["functionDeclarations"][0]
    assert decl["name"] == "read_file"
    assert decl["parametersJsonSchema"] == TOOLS[0]["function"]["parameters"]
    assert body["generationConfig"] == {"maxOutputTokens": 65536, "temperature": 0.2}

    assert [e["text"] for e in out if e["type"] == "token"] == ["Hi ", "there"]
    final = out[-1]
    assert final["content"] == "Hi there"
    assert final["tool_calls"][0]["function"] == {
        "name": "read_file", "arguments": json.dumps({"path": "a.md"})}
    assert final["usage"] == {"prompt_tokens": 1000, "completion_tokens": 42,
                              "prompt_cache_hit_tokens": 600,
                              "prompt_cache_miss_tokens": 400}


async def test_replay_keeps_signatures_and_names_tool_results(gemini):
    first = (await _drain(ModelGateway().complete(
        [{"role": "user", "content": "go"}], tools=TOOLS, model_name=MODEL)))[-1]
    call_id = first["tool_calls"][0]["id"]
    await _drain(ModelGateway().complete([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": first["content"],
         "tool_calls": first["tool_calls"], "provider_blocks": first["provider_blocks"]},
        {"role": "tool", "tool_call_id": call_id, "content": "file body"},
    ], tools=TOOLS, model_name=MODEL))
    contents = json.loads(gemini[1].content)["contents"]
    assert contents[1] == {"role": "model", "parts": first["provider_blocks"]["parts"]}
    assert contents[1]["parts"][-1]["thoughtSignature"] == "SIG1"
    assert contents[2] == {"role": "user", "parts": [{"functionResponse": {
        "name": "read_file", "response": {"result": "file body"}}}]}


def test_foreign_tool_calls_get_the_placeholder_signature():
    _, contents = adapters.to_gemini([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_x", "type": "function",
            "function": {"name": "t", "arguments": "{\"a\": 1}"}}]},
        {"role": "tool", "tool_call_id": "call_x", "content": "ok"}])
    assert contents[1]["parts"] == [{"functionCall": {"name": "t", "args": {"a": 1}},
                                     "thoughtSignature": adapters.DUMMY_THOUGHT_SIGNATURE}]
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "t"
