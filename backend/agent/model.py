"""The single model choke point: every LLM call goes through Model.complete,
and the peak-cost gate lives in front of it. `providers.resolve` routes each
call — `provider/model` -> endpoint, key, wire kind — and the non-OpenAI
wire formats live in adapters.py."""
import json
import re
import time
from datetime import datetime, time as dtime
from typing import AsyncIterator

from ..config import settings
from .. import providers
from ..providers import base_url_allowed, endpoint as _endpoint  # noqa: F401 (re-export)
from . import adapters, budget as budget_mod
from .adapters import ModelError, retrying


def _is_deepseek_endpoint(url: str) -> bool:
    """DeepSeek's quirks (DSML recovery) apply to calls going to its host."""
    return _endpoint(url)[1] == _endpoint(settings.deepseek_base_url)[1]


def _is_voice_local(name: str, base: str) -> bool:
    """This exact call is the voice fast tier — the operator's own model on the
    operator's own endpoint. Narrow on purpose: the sampling below is tuned for
    a 4B speaking one or two sentences and must not touch DeepSeek, or an agent
    pinned to some other local model."""
    return bool(settings.voice_local_model) and name == settings.voice_local_model \
        and _endpoint(base) == _endpoint(settings.voice_local_base_url)


# deepseek-v4-flash sometimes emits tool calls in its native markup as plain
# TEXT instead of the structured tool_calls field, so the serving layer doesn't
# parse them and they arrive as garbage content (the tool never runs). Recover
# them: parse the markup back into tool_calls. The '｜' below is U+FF5C.
_DSML_MARK = "DSML"
_DSML_INVOKE = re.compile(
    r'<｜｜DSML｜｜invoke name="([^"]+)">(.*?)</｜｜DSML｜｜invoke>', re.S)
_DSML_PARAM = re.compile(
    r'<｜｜DSML｜｜parameter name="([^"]+)"[^>]*>(.*?)</｜｜DSML｜｜parameter>', re.S)


def _coerce(v: str):
    s = v.strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if s in ("true", "false"):
        return s == "true"
    return v


def parse_dsml_tool_calls(content: str) -> list[dict]:
    """Recover tool calls the model emitted as text markup instead of structured
    fields. Returns [] if there are none."""
    calls = []
    for i, m in enumerate(_DSML_INVOKE.finditer(content)):
        args = {p.group(1): _coerce(p.group(2)) for p in _DSML_PARAM.finditer(m.group(2))}
        calls.append({"id": f"dsml_{i}", "type": "function",
                      "function": {"name": m.group(1), "arguments": json.dumps(args)}})
    return calls


class PeakPricingConfirmationRequired(Exception):
    """Raised when a call lands inside a peak-pricing window and the user
    hasn't confirmed they want to pay 2x for this conversation recently."""


def _parse_window(spec: str) -> tuple[dtime, dtime]:
    start_s, end_s = spec.split("-")
    h1, m1 = (int(x) for x in start_s.split(":"))
    h2, m2 = (int(x) for x in end_s.split(":"))
    return dtime(h1, m1), dtime(h2, m2)


def in_peak_window(now: datetime | None = None, windows: list[str] | None = None) -> bool:
    now = now or datetime.now()
    t = now.time()
    for spec in windows if windows is not None else settings.peak_windows:
        start, end = _parse_window(spec)
        if start <= end:
            if start <= t < end:
                return True
        else:  # crosses midnight, e.g. 23:00-03:00
            if t >= start or t < end:
                return True
    return False


# conversation_id -> unix time the user last confirmed peak usage
_peak_confirmations: dict[int, float] = {}


def confirm_peak(conversation_id: int) -> None:
    _peak_confirmations[conversation_id] = time.time()


def peak_confirmed(conversation_id: int) -> bool:
    ts = _peak_confirmations.get(conversation_id)
    return ts is not None and time.time() - ts < settings.peak_confirm_ttl_minutes * 60


def check_peak_gate(conversation_id: int) -> None:
    if in_peak_window() and not peak_confirmed(conversation_id):
        raise PeakPricingConfirmationRequired()


CAPTURE_STATE_KEY = "capture_context"


def _redact_images(messages: list[dict]) -> list[dict]:
    """Swap base64 image data-URIs for a short placeholder before a message
    array is logged — a captured screenshot is multi-MB and would bloat the
    model_calls ledger with bytes that add nothing to the debug view."""
    out = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "image_url":
                url = (p.get("image_url") or {}).get("url", "")
                parts.append({"type": "image_url", "image_url":
                              {"url": f"<image redacted: {len(url):,} chars>"}})
            else:
                parts.append(p)
        out.append({**m, "content": parts})
    return out


async def record_model_call(conversation_id: int | None, model_name: str,
                            usage: dict | None, messages: list[dict],
                            tools: list[dict] | None) -> None:
    """Ledger every API call: exact usage always (the Logs cost tab sums
    this — usage_log only covers chat turns, this covers everything), plus
    the raw message array when the operator flipped capture on. Incognito
    records usage unattributed (spend is real money) but never content.
    Must never fail the model call — best effort by design."""
    from ..db import get_db, get_state
    from .. import runtime
    u = usage or {}
    ephemeral = runtime.ephemeral.get()
    if ephemeral:
        conversation_id = None
    db = await get_db()
    try:
        context = None
        if not ephemeral and await get_state(db, CAPTURE_STATE_KEY) == "1":
            context = json.dumps({"messages": _redact_images(messages),
                                  "n_tools": len(tools or [])})
        await db.execute(
            "INSERT INTO model_calls (conversation_id, model, input_tokens, "
            "output_tokens, cache_hit, cache_miss, context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, model_name,
             u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
             u.get("prompt_cache_hit_tokens", 0),
             u.get("prompt_cache_miss_tokens", 0), context))
        # retention: usage rows are tiny and kept forever; context blobs are
        # the heavy part and age out
        await db.execute(
            "UPDATE model_calls SET context = NULL WHERE context IS NOT NULL "
            "AND created_at < datetime('now', ?)",
            (f"-{settings.context_capture_keep_days} days",))
        await db.commit()
    finally:
        await db.close()


def _openai_messages(messages: list[dict]) -> list[dict]:
    """Drop the opaque per-provider replay state (adapters.py) — an
    OpenAI-compatible endpoint may reject unknown message fields."""
    if not any("provider_blocks" in m for m in messages):
        return messages
    return [{k: v for k, v in m.items() if k != "provider_blocks"} for m in messages]


class ModelClient:
    """Pure transport to an OpenAI-compatible chat-completions endpoint (every
    provider of kind openai/ollama — DeepSeek, OpenAI, OpenRouter, Groq, ...):
    it builds the request, streams it (with retry + DSML recovery), and yields
    events. It holds NO key policy, budget, peak gate, or ledger — those are the
    host nucleus (ModelGateway). The auth key is passed in per call, so this
    layer can run keyless when a gateway drives it (the VM-inversion seam).
    Per-provider request shape (output cap, sampling) comes from the read-only
    catalogue entry of whatever provider lives at the base_url."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.deepseek_api_key
        self.base_url = settings.deepseek_base_url.rstrip("/")
        self.name = settings.model_name

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        model_name: str | None = None,
        base_url: str | None = None,
        key: str | None = None,
    ) -> AsyncIterator[dict]:
        """Stream {"type": "token", "text": str} per delta, then one
        {"type": "message", "content", "tool_calls", "usage"} with any DSML
        tool-call markup already recovered. Transport only — no metering, no
        gate; those live in ModelGateway."""
        base = (base_url or self.base_url).rstrip("/")
        name = model_name or self.name
        key = key or self.api_key

        payload: dict = {
            "model": name,
            "messages": _openai_messages(messages),
            "max_tokens": settings.model_max_tokens,
            "temperature": settings.model_temperature if temperature is None else temperature,
            "stream": True,
            # ask for a final usage chunk so we can meter tokens + cache hits
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
        _shape_for_provider(payload, base, name)
        if _is_voice_local(name, base):
            # A 4B answering out loud needs a few dozen tokens, not 384k (which
            # is also nonsense against a 16k window), and it will happily loop
            # on its own last phrasing — the operator's "it keeps saying the
            # same stuff". The structural half of that fix is replaying tool
            # turns into the history (compaction._with_tool_trace); this is the
            # sampling half.
            payload["max_tokens"] = settings.voice_local_max_tokens
            if settings.voice_local_presence_penalty:
                payload["presence_penalty"] = settings.voice_local_presence_penalty

        # Transient failures (connect errors, 5xx) retry with backoff — but only
        # while nothing has streamed to the caller yet (adapters.retrying).
        raw: dict | None = None
        async for ev in retrying(lambda: self._stream_once(base, key, payload)):
            if ev["type"] == "token":
                yield ev
            else:
                raw = ev

        assert raw is not None
        content = raw["content"]
        tcs = raw["tool_calls"]
        # recover native-markup tool calls the serving layer failed to parse —
        # DeepSeek's quirk only; another provider's text is left alone
        if not tcs and _DSML_MARK in content and _is_deepseek_endpoint(base):
            recovered = parse_dsml_tool_calls(content)
            if recovered:
                tcs = recovered
                content = ""   # the markup was the tool call, not a message
        yield {"type": "message", "content": content, "tool_calls": tcs,
               "usage": raw["usage"]}

    async def _stream_once(self, base: str, key: str, payload: dict) -> AsyncIterator[dict]:
        """One streaming HTTP attempt: token events, then a single raw
        {"type": "raw", "content", "tool_calls", "usage"} accumulation."""
        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        usage: dict | None = None
        dsml = False   # once the native tool-call markup starts, stop streaming it
        watch_dsml = _is_deepseek_endpoint(base)
        tail = ""      # rolling window for mark detection across chunk splits —
                       # re-joining content_parts per delta was O(n²) per response

        async with adapters.client() as client:
            async with client.stream(
                "POST",
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    raise ModelError(f"model API {resp.status_code}: {body[:500]}",
                                     status=resp.status_code)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    obj = json.loads(data)
                    if obj.get("usage"):        # final include_usage chunk
                        usage = obj["usage"]
                    choices = obj.get("choices") or []
                    if not choices:             # usage-only chunk has no choices
                        continue
                    delta = choices[0].get("delta", {})
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                        if not dsml and watch_dsml:
                            probe = tail + delta["content"]
                            if _DSML_MARK in probe:
                                dsml = True   # a tool call in disguise, not prose
                            tail = probe[-(len(_DSML_MARK) - 1):]
                        if not dsml:
                            yield {"type": "token", "text": delta["content"]}
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls.setdefault(
                            idx, {"id": "", "type": "function",
                                  "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]

        yield {"type": "raw", "content": "".join(content_parts),
               "tool_calls": [tool_calls[i] for i in sorted(tool_calls)],
               "usage": usage}


def _shape_for_provider(payload: dict, base: str, name: str) -> None:
    """Fit the OpenAI-shaped request to the provider at `base` (catalogue
    data only — no keys here). DeepSeek and unknown endpoints keep today's
    shape; everyone else gets the model's own output cap instead of DeepSeek's
    384k, no temperature where the model rejects sampling, OpenAI's newer
    max_completion_tokens, and no stream_options where it isn't accepted."""
    p = providers.provider_for_base(base)
    if p is None or p["id"] == "deepseek":
        return
    info = (p.get("_models") or {}).get(name) or {}
    cap = info.get("max_output")
    payload.pop("max_tokens")
    if cap:
        payload["max_completion_tokens" if p["id"] == "openai" else "max_tokens"] = \
            min(cap, settings.model_max_tokens)
    if info.get("temperature") is False:
        payload.pop("temperature", None)
    if p["id"] == "mistral":
        payload.pop("stream_options", None)   # reports usage on the last chunk anyway


# Back-compat alias: tests construct Model(api_key=...) and patch Model._stream_once.
Model = ModelClient


def _normalise_usage(usage: dict | None) -> dict | None:
    """OpenAI-style providers report cached input as
    prompt_tokens_details.cached_tokens; the Budget and ledger read DeepSeek's
    prompt_cache_hit/miss_tokens. Fill those in when only the former exists."""
    if not usage or "prompt_cache_hit_tokens" in usage:
        return usage
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is None:
        return usage
    prompt = usage.get("prompt_tokens") or 0
    return {**usage, "prompt_cache_hit_tokens": cached,
            "prompt_cache_miss_tokens": max(prompt - cached, 0)}


def _cache_weight(route) -> float | None:
    """What a cached input token costs relative to a fresh one, for this
    model — the Budget's spend proxy. None = the Budget's default."""
    info = route.info or {}
    pin, pc = info.get("price_in"), info.get("price_cache")
    if pin and pc is not None:
        return pc / pin
    return None


class ModelGateway:
    """The host nucleus in front of the transport: the one place that holds the
    API-key policy, routes a call to its provider, enforces the peak-pricing
    gate, meters the shared token Budget, and writes the model_calls ledger.
    `complete(...)` keeps the exact public contract every caller relies on
    (token events, then one message event). Wrapping the transport this way is
    the seam the VM inversion splits along — the transport can move guest-side
    while this stays on the host."""

    def __init__(self, api_key: str | None = None):
        # None = resolve the deepseek key per call (secrets store, then the
        # env); a string (tests, "" = none) pins it
        self.api_key = api_key
        self.transport = ModelClient(api_key=api_key)

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        conversation_id: int | None = None,
        temperature: float | None = None,
        model_name: str | None = None,
        base_url: str | None = None,
        op_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Stream events: {"type": "token", "text": str} per delta, then one
        {"type": "message", "content", "tool_calls", "usage"} (+ an opaque
        `provider_blocks` for adapters that need replay state). Raises
        PeakPricingConfirmationRequired / BudgetExceeded / ModelError before any
        network I/O.

        model_name is `provider/model` (a bare id runs on the default model's
        provider); None = the default model. base_url pins an allowlisted
        endpoint (an agent on a local ollama, the voice tier) — the key sent is
        that endpoint's own provider's, else "local" (providers.resolve).

        The token budget is resolved by op_id (an explicit id, else the operation
        in scope via the active_op_id contextvar) so enforcement is keyed, not
        ambient — the seam Phase 3 uses to meter host-side across the VM boundary."""
        try:
            route = providers.resolve(model_name, base_url, deepseek_key=self.api_key)
        except providers.ProviderError as e:
            raise ModelError(str(e)) from None
        # the peak gate prices DEEPSEEK hours — other providers (and a local
        # ollama) cost the same at any hour, so only DeepSeek calls are gated
        if conversation_id is not None and route.is_deepseek:
            check_peak_gate(conversation_id)
        budget = budget_mod.get(op_id) if op_id else budget_mod.current()
        if budget is not None and budget.over():
            raise budget_mod.BudgetExceeded(
                f"token budget spent ({budget.summary()})")
        if route.key_error:
            raise ModelError(route.key_error)

        if route.kind == "anthropic":
            stream = adapters.anthropic_complete(route, messages, tools, temperature)
        elif route.kind == "google":
            stream = adapters.google_complete(route, messages, tools, temperature)
        else:
            base = route.base_url
            if route.kind == "ollama" and not base.endswith("/v1"):
                base += "/v1"          # ollama's OpenAI-compatible surface
            stream = self.transport.complete(
                messages, tools=tools, temperature=temperature,
                model_name=route.model, base_url=base, key=route.key)

        final: dict | None = None
        try:
            async for ev in stream:
                if ev["type"] == "token":
                    yield ev
                else:
                    final = ev
        except ModelError as e:
            # an error body that echoes the request must not carry the key
            # into the transcript, the logs or the guest
            if route.key and len(route.key) >= 6 and route.key in str(e):
                raise ModelError(str(e).replace(route.key, "***"),
                                 status=e.status) from None
            raise

        assert final is not None
        usage = _normalise_usage(final["usage"])
        final = {**final, "usage": usage}
        if budget is not None:
            budget.add(usage or {}, cache_weight=_cache_weight(route))
        try:
            await record_model_call(conversation_id, route.model_id, usage,
                                    messages, tools)
        except Exception:  # noqa: BLE001 — the ledger must never fail a call
            pass
        yield final


model = ModelGateway()


async def complete_text(system: str, user: str, temperature: float = 0.3) -> str:
    """Drain a no-tools `system + user -> text` model call to a single string —
    the common helper shared by summarize / research / the funnel. Runs through
    the same `model.complete` choke point, so it shares the operation's Budget
    contextvar and is metered like any other call."""
    parts = []
    async for ev in model.complete(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}], temperature=temperature):
        if ev["type"] == "message":
            parts.append(ev["content"] or "")
    return "".join(parts).strip()
