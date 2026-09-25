"""Wire adapters for the providers that don't speak OpenAI chat-completions.

The whole agent — loop, compaction, tools — speaks one message shape (OpenAI
chat: system/user/assistant(+tool_calls)/tool) and consumes one event shape
from the model gateway: {"type": "token", "text"} per delta, then one
{"type": "message", "content", "tool_calls", "usage"}. An adapter translates
that shape to a provider's native API and its stream back, so nothing above
the gateway knows which provider answered.

`usage` is normalised to the DeepSeek/OpenAI field names the Budget and the
ledger read (prompt_tokens, completion_tokens, prompt_cache_hit_tokens,
prompt_cache_miss_tokens). A provider's opaque per-turn state that must be
replayed verbatim on the next request of the same tool loop (Anthropic
thinking blocks + their signatures, Gemini thought signatures) rides on the
message event as `provider_blocks`; loop.py stores it on the assistant
message, the adapter replays it, and the OpenAI path strips it.

Pure transport: no keys are looked up here (the Route carries the one the
gateway resolved), no budget, no ledger.
"""
import asyncio
import copy
import json
from typing import AsyncIterator, Callable

import httpx

from ..config import settings
from .. import providers

HTTP_TRANSPORT = None       # tests swap in an httpx.MockTransport
DEFAULT_MAX_OUTPUT = 32_000  # an uncatalogued model's output cap


class ModelError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15),
                             transport=HTTP_TRANSPORT)


async def retrying(once: Callable[[], AsyncIterator[dict]]) -> AsyncIterator[dict]:
    """Run one streaming attempt, retrying transient failures (connect errors,
    5xx) with backoff — but only while nothing has streamed to the caller: once
    a token is out a retry would duplicate visible output, so the error
    propagates. Yields token events, then the attempt's single final event."""
    yielded = False
    for attempt in range(settings.model_retries + 1):
        try:
            async for ev in once():
                if ev["type"] == "token":
                    yielded = True
                yield ev
            return
        except (httpx.TransportError, ModelError) as e:
            status = getattr(e, "status", None)
            retryable = isinstance(e, httpx.TransportError) or (
                status is not None and status >= 500)
            if yielded or not retryable or attempt == settings.model_retries:
                raise
            await asyncio.sleep(settings.model_retry_backoff_seconds * (2 ** attempt))


def _max_output(route) -> int:
    cap = (route.info or {}).get("max_output") or DEFAULT_MAX_OUTPUT
    return min(cap, settings.model_max_tokens)


def _temperature(route, temperature: float | None) -> float | None:
    """Only catalogued models that accept sampling get a temperature — the
    current Claude line 400s on any, and an unknown model is not worth the
    risk."""
    info = route.info or {}
    if not info or info.get("temperature") is False:
        return None
    return settings.model_temperature if temperature is None else temperature


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def _data_uri(url: str) -> tuple[str, str] | None:
    """('image/png', base64) for a data: URI, else None."""
    if not url.startswith("data:") or ";base64," not in url:
        return None
    head, data = url[5:].split(";base64,", 1)
    return head or "image/png", data


def _args(tc: dict) -> dict:
    raw = (tc.get("function") or {}).get("arguments") or "{}"
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {"value": out}
    except json.JSONDecodeError:
        return {"_raw": raw}


def _carried(m: dict, kind: str):
    pb = m.get("provider_blocks")
    return pb if isinstance(pb, dict) and pb.get("kind") == kind else None


def _http_error(resp_status: int, body: str, provider: str) -> ModelError:
    return ModelError(f"{provider} API {resp_status}: {body[:500]}", status=resp_status)


# --- Anthropic Messages API ---------------------------------------------------

def _anthropic_user_blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and p.get("text"):
            out.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url", "")
            d = _data_uri(url)
            if d:
                out.append({"type": "image", "source": {
                    "type": "base64", "media_type": d[0], "data": d[1]}})
            elif url:
                out.append({"type": "image", "source": {"type": "url", "url": url}})
    return out


def to_anthropic(messages: list[dict], keep_thinking: bool = True) -> tuple[str, list[dict]]:
    """OpenAI-shaped history -> (system, messages). Consecutive same-role turns
    merge (tool results become one user turn, as parallel tool use needs);
    a turn this adapter produced replays its original blocks unchanged."""
    system: list[str] = []
    out: list[dict] = []

    def push(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m.get("role")
        if role == "system":
            t = _text_of(m.get("content"))
            if t:
                system.append(t)
        elif role == "user":
            push("user", _anthropic_user_blocks(m.get("content"))
                 or [{"type": "text", "text": "(empty)"}])
        elif role == "assistant":
            pb = _carried(m, "anthropic")
            if pb and pb.get("content"):
                blocks = copy.deepcopy(pb["content"])
                if not keep_thinking:
                    blocks = [b for b in blocks
                              if b.get("type") not in ("thinking", "redacted_thinking")]
            else:
                blocks = []
                text = _text_of(m.get("content"))
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": tc.get("id") or "call",
                                   "name": (tc.get("function") or {}).get("name", ""),
                                   "input": _args(tc)})
            push("assistant", blocks)
        elif role == "tool":
            content = m.get("content")
            result = _anthropic_user_blocks(content) if isinstance(content, list) \
                else (content or "")
            push("user", [{"type": "tool_result",
                           "tool_use_id": m.get("tool_call_id") or "call",
                           "content": result}])
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    return "\n\n".join(system), out


def anthropic_tools(tools: list[dict] | None) -> list[dict]:
    out = []
    for t in tools or []:
        fn = t.get("function") or t
        out.append({"name": fn.get("name", ""),
                    "description": fn.get("description") or "",
                    "input_schema": fn.get("parameters")
                    or {"type": "object", "properties": {}}})
    return out


def _anthropic_usage(start: dict, delta: dict) -> dict:
    u = {**start, **{k: v for k, v in delta.items() if v is not None}}
    fresh = u.get("input_tokens") or 0
    read = u.get("cache_read_input_tokens") or 0
    write = u.get("cache_creation_input_tokens") or 0
    return {"prompt_tokens": fresh + read + write,
            "completion_tokens": u.get("output_tokens") or 0,
            "prompt_cache_hit_tokens": read,
            "prompt_cache_miss_tokens": fresh + write}


async def _anthropic_once(route, payload: dict) -> AsyncIterator[dict]:
    blocks: dict[int, dict] = {}
    start_usage: dict = {}
    delta_usage: dict = {}
    stop = None
    headers = {**providers.auth_headers({"kind": "anthropic"}, route.key),
               "content-type": "application/json"}
    async with client() as c:
        async with c.stream("POST", f"{route.base_url}/messages",
                            headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")
                raise _http_error(resp.status_code, body, "anthropic")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:].strip() or "{}")
                t = ev.get("type")
                if t == "message_start":
                    start_usage = (ev.get("message") or {}).get("usage") or {}
                elif t == "content_block_start":
                    b = dict(ev.get("content_block") or {})
                    if b.get("type") == "tool_use":
                        b["_json"] = ""
                    blocks[ev.get("index", len(blocks))] = b
                elif t == "content_block_delta":
                    b = blocks.setdefault(ev.get("index", 0), {"type": "text", "text": ""})
                    d = ev.get("delta") or {}
                    dt = d.get("type")
                    if dt == "text_delta":
                        b["text"] = b.get("text", "") + d.get("text", "")
                        if d.get("text"):
                            yield {"type": "token", "text": d["text"]}
                    elif dt == "input_json_delta":
                        b["_json"] = b.get("_json", "") + d.get("partial_json", "")
                    elif dt == "thinking_delta":
                        b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
                    elif dt == "signature_delta":
                        b["signature"] = d.get("signature", "")
                elif t == "message_delta":
                    delta_usage = ev.get("usage") or {}
                    stop = (ev.get("delta") or {}).get("stop_reason") or stop
                elif t == "error":
                    err = ev.get("error") or {}
                    status = 529 if err.get("type") == "overloaded_error" else 500
                    raise ModelError(f"anthropic stream error: {err.get('message') or err}",
                                     status=status)
                elif t == "message_stop":
                    break

    content_blocks, texts, calls = [], [], []
    for i in sorted(blocks):
        b = blocks[i]
        if b.get("type") == "tool_use":
            raw = b.pop("_json", "")
            if raw:
                try:
                    b["input"] = json.loads(raw)
                except json.JSONDecodeError:
                    b["input"] = {"_raw": raw}
            b.setdefault("input", {})
            calls.append({"id": b.get("id", f"call_{i}"), "type": "function",
                          "function": {"name": b.get("name", ""),
                                       "arguments": json.dumps(b["input"])}})
        elif b.get("type") == "text":
            if not b.get("text"):
                continue          # the API rejects empty text blocks on replay
            texts.append(b["text"])
        content_blocks.append(b)
    content = "".join(texts)
    if stop == "refusal" and not content and not calls:
        content = "(the model declined this request)"
    yield {"type": "raw", "content": content, "tool_calls": calls,
           "usage": _anthropic_usage(start_usage, delta_usage),
           "provider_blocks": {"kind": "anthropic", "model": route.model_id,
                               "content": content_blocks}}


async def anthropic_complete(route, messages: list[dict], tools: list[dict] | None,
                             temperature: float | None) -> AsyncIterator[dict]:
    """Stream a Messages API call as the gateway's event shape."""
    def payload(keep_thinking: bool) -> dict:
        system, msgs = to_anthropic(messages, keep_thinking)
        p = {"model": route.model, "max_tokens": _max_output(route),
             "messages": msgs, "stream": True}
        if system:
            p["system"] = system
        if tools:
            p["tools"] = anthropic_tools(tools)
        t = _temperature(route, temperature)
        if t is not None:
            p["temperature"] = t
        if route.provider == "anthropic":
            # automatic prompt caching: the ReAct loop re-sends a growing,
            # stable prefix every iteration
            p["cache_control"] = {"type": "ephemeral"}
        return p

    body = payload(True)
    try:
        async for ev in retrying(lambda: _anthropic_once(route, body)):
            if ev["type"] == "token":
                yield ev
            else:
                final = ev
    except ModelError as e:
        # Replayed thinking blocks are bound to the conversation prefix; Jav3
        # edits history (compaction, rule injection), so a rejected block is
        # recovered the documented way: strip them all and retry once.
        msg = str(e)
        if e.status != 400 or not ("thinking" in msg or "signature" in msg) \
                or not any(_carried(m, "anthropic") for m in messages):
            raise
        body = payload(False)
        async for ev in retrying(lambda: _anthropic_once(route, body)):
            if ev["type"] == "token":
                yield ev
            else:
                final = ev
    yield {**final, "type": "message"}


# --- Google Gemini (generateContent) ------------------------------------------

# Gemini 3 requires the thought signature of every function call replayed in
# the current turn; a call that came from another provider (a mid-chat model
# switch) has none, and this is Google's documented placeholder for that case.
DUMMY_THOUGHT_SIGNATURE = "skip_thought_signature_validator"


def _gemini_parts(content) -> list[dict]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    out = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and p.get("text"):
            out.append({"text": p["text"]})
        elif p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url", "")
            d = _data_uri(url)
            out.append({"inlineData": {"mimeType": d[0], "data": d[1]}} if d
                       else {"text": f"[image: {url}]"})
    return out


def to_gemini(messages: list[dict]) -> tuple[str, list[dict]]:
    system: list[str] = []
    out: list[dict] = []
    names: dict[str, str] = {}

    def push(role: str, parts: list[dict]) -> None:
        if not parts:
            return
        if out and out[-1]["role"] == role:
            out[-1]["parts"].extend(parts)
        else:
            out.append({"role": role, "parts": list(parts)})

    for m in messages:
        role = m.get("role")
        for tc in m.get("tool_calls") or []:
            names[tc.get("id") or ""] = (tc.get("function") or {}).get("name", "")
        if role == "system":
            t = _text_of(m.get("content"))
            if t:
                system.append(t)
        elif role == "user":
            push("user", _gemini_parts(m.get("content")) or [{"text": "(empty)"}])
        elif role == "assistant":
            pb = _carried(m, "google")
            if pb and pb.get("parts"):
                parts = copy.deepcopy(pb["parts"])
            else:
                parts = _gemini_parts(_text_of(m.get("content")))
                for tc in m.get("tool_calls") or []:
                    parts.append({"functionCall": {
                        "name": (tc.get("function") or {}).get("name", ""),
                        "args": _args(tc)},
                        "thoughtSignature": DUMMY_THOUGHT_SIGNATURE})
            push("model", parts)
        elif role == "tool":
            cid = m.get("tool_call_id") or ""
            push("user", [{"functionResponse": {
                "name": names.get(cid) or m.get("name") or "tool",
                "response": {"result": _text_of(m.get("content"))}}}])
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "parts": [{"text": "(continue)"}]})
    return "\n\n".join(system), out


def gemini_tools(tools: list[dict] | None) -> list[dict]:
    decls = []
    for t in tools or []:
        fn = t.get("function") or t
        decls.append({"name": fn.get("name", ""),
                      "description": fn.get("description") or "",
                      "parametersJsonSchema": fn.get("parameters")
                      or {"type": "object", "properties": {}}})
    return [{"functionDeclarations": decls}] if decls else []


def _gemini_usage(u: dict) -> dict:
    prompt = u.get("promptTokenCount") or 0
    cached = u.get("cachedContentTokenCount") or 0
    return {"prompt_tokens": prompt,
            "completion_tokens": (u.get("candidatesTokenCount") or 0)
            + (u.get("thoughtsTokenCount") or 0),
            "prompt_cache_hit_tokens": cached,
            "prompt_cache_miss_tokens": max(prompt - cached, 0)}


async def _gemini_once(route, payload: dict) -> AsyncIterator[dict]:
    parts: list[dict] = []
    usage: dict = {}
    finish = None
    headers = {**providers.auth_headers({"kind": "google"}, route.key),
               "content-type": "application/json"}
    url = f"{route.base_url}/models/{route.model}:streamGenerateContent"
    async with client() as c:
        async with c.stream("POST", url, params={"alt": "sse"},
                            headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")
                raise _http_error(resp.status_code, body, "gemini")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = json.loads(line[5:].strip() or "{}")
                if chunk.get("error"):
                    err = chunk["error"]
                    raise ModelError(f"gemini stream error: {err.get('message') or err}",
                                     status=err.get("code") or 500)
                usage = chunk.get("usageMetadata") or usage
                for cand in (chunk.get("candidates") or [])[:1]:
                    finish = cand.get("finishReason") or finish
                    for p in (cand.get("content") or {}).get("parts") or []:
                        parts.append(p)
                        if p.get("text") and not p.get("thought"):
                            yield {"type": "token", "text": p["text"]}

    texts, calls = [], []
    for i, p in enumerate(parts):
        if p.get("functionCall"):
            fc = p["functionCall"]
            calls.append({"id": fc.get("id") or f"call_{i}", "type": "function",
                          "function": {"name": fc.get("name", ""),
                                       "arguments": json.dumps(fc.get("args") or {})}})
        elif p.get("text") and not p.get("thought"):
            texts.append(p["text"])
    content = "".join(texts)
    if not content and not calls and finish not in (None, "STOP"):
        content = f"(the model stopped: {finish})"
    yield {"type": "raw", "content": content, "tool_calls": calls,
           "usage": _gemini_usage(usage),
           "provider_blocks": {"kind": "google", "model": route.model_id,
                               "parts": parts}}


async def google_complete(route, messages: list[dict], tools: list[dict] | None,
                          temperature: float | None) -> AsyncIterator[dict]:
    system, contents = to_gemini(messages)
    payload: dict = {"contents": contents}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        payload["tools"] = gemini_tools(tools)
    gen = {"maxOutputTokens": _max_output(route)}
    t = _temperature(route, temperature)
    if t is not None:
        gen["temperature"] = t
    payload["generationConfig"] = gen
    final = None
    async for ev in retrying(lambda: _gemini_once(route, payload)):
        if ev["type"] == "token":
            yield ev
        else:
            final = ev
    yield {**final, "type": "message"}
