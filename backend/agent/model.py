"""The single model choke point: every LLM call goes through Model.complete,
and the peak-cost gate lives in front of it. The router drops in here later."""
import asyncio
import json
import re
import time
from datetime import datetime, time as dtime
from typing import AsyncIterator

import httpx

from urllib.parse import urlsplit

from ..config import settings
from . import budget as budget_mod


def _endpoint(url: str) -> tuple:
    p = urlsplit(url if "://" in url else "http://" + url)
    return (p.scheme, (p.hostname or "").lower(), p.port)


def base_url_allowed(url: str) -> bool:
    """A guest-supplied model base_url is honoured only if it matches the
    configured DeepSeek endpoint or one on model_base_url_allowlist. The host
    attaches the API key to the request, so an unchecked base_url lets a
    compromised guest harvest the key by naming an attacker endpoint."""
    t = _endpoint(url)
    return any(_endpoint(a) == t
               for a in [settings.deepseek_base_url, *settings.model_base_url_allowlist])


def _is_deepseek_endpoint(url: str) -> bool:
    return _endpoint(url)[1] == _endpoint(settings.deepseek_base_url)[1]


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


class ModelError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


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
MODEL_STATE_KEY = "model_override"

# Runtime model switch (nav dropdown): one host-side slot consulted by the
# gateway, so chat, agents, and guest turns all follow it. An explicit
# per-call model_name (agent pin) always wins. Persisted in session_state and
# reloaded at app startup.
_model_override: str | None = None


def get_model_override() -> str | None:
    return _model_override


def set_model_override(name: str | None) -> None:
    global _model_override
    _model_override = name or None


async def load_model_override() -> None:
    from ..db import get_db, get_state
    db = await get_db()
    try:
        set_model_override(await get_state(db, MODEL_STATE_KEY))
    finally:
        await db.close()


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
            context = json.dumps({"messages": messages,
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


class ModelClient:
    """Pure transport to the OpenAI-compatible chat-completions endpoint: it
    builds the request, streams it (with retry + DSML recovery), and yields
    events. It holds NO key policy, budget, peak gate, or ledger — those are the
    host nucleus (ModelGateway). The auth key is passed in per call, so this
    layer can run keyless when a gateway drives it (the VM-inversion seam)."""

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
            "messages": messages,
            "max_tokens": settings.model_max_tokens,
            "temperature": settings.model_temperature if temperature is None else temperature,
            "stream": True,
            # ask for a final usage chunk so we can meter tokens + cache hits
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools

        # Transient failures (connect errors, 5xx) retry with backoff — but only
        # while nothing has streamed to the caller yet: once a token is out, a
        # retry would duplicate visible output, so the error propagates instead.
        raw: dict | None = None
        yielded = False
        for attempt in range(settings.model_retries + 1):
            try:
                async for ev in self._stream_once(base, key, payload):
                    if ev["type"] == "token":
                        yielded = True
                        yield ev
                    else:
                        raw = ev
                break
            except (httpx.TransportError, ModelError) as e:
                status = getattr(e, "status", None)
                retryable = isinstance(e, httpx.TransportError) or (
                    status is not None and status >= 500)
                if yielded or not retryable or attempt == settings.model_retries:
                    raise
                await asyncio.sleep(
                    settings.model_retry_backoff_seconds * (2 ** attempt))

        assert raw is not None
        content = raw["content"]
        tcs = raw["tool_calls"]
        # recover native-markup tool calls the serving layer failed to parse
        if not tcs and _DSML_MARK in content:
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
        tail = ""      # rolling window for mark detection across chunk splits —
                       # re-joining content_parts per delta was O(n²) per response

        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
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
                        if not dsml:
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


# Back-compat alias: tests construct Model(api_key=...) and patch Model._stream_once.
Model = ModelClient


class ModelGateway:
    """The host nucleus in front of the transport: the one place that holds the
    API-key policy, enforces the peak-pricing gate, meters the shared token
    Budget, and writes the model_calls ledger. `complete(...)` keeps the exact
    public contract every caller relies on (token events, then one message
    event). Wrapping the transport this way is the seam the VM inversion splits
    along — the transport can move guest-side while this stays on the host."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.deepseek_api_key
        self.transport = ModelClient(api_key=self.api_key)

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
        {"type": "message", "content", "tool_calls", "usage"}. Raises
        PeakPricingConfirmationRequired / BudgetExceeded before any network I/O.

        The token budget is resolved by op_id (an explicit id, else the operation
        in scope via the active_op_id contextvar) so enforcement is keyed, not
        ambient — the seam Phase 3 uses to meter host-side across the VM boundary.

        model_name/base_url override the defaults so an agent can run on a
        different model or a local endpoint (e.g. ollama). A custom endpoint
        usually needs no key, so the DeepSeek-key requirement is relaxed there."""
        # the peak gate prices DEEPSEEK hours — a local endpoint (ollama) costs
        # nothing at any hour, so only metered calls are gated
        if conversation_id is not None and (
                not base_url or _is_deepseek_endpoint(base_url)):
            check_peak_gate(conversation_id)
        budget = budget_mod.get(op_id) if op_id else budget_mod.current()
        if budget is not None and budget.over():
            raise budget_mod.BudgetExceeded(
                f"token budget spent ({budget.summary()})")
        # key policy: a custom endpoint (ollama etc.) may need no real key. The
        # HOST attaches the key, so a guest-supplied base_url is a key-exfil seam
        # — reject anything off the allowlist, and send the real key ONLY to the
        # configured DeepSeek endpoint (a local ollama is sent "local", not the key).
        if base_url:
            if not base_url_allowed(base_url):
                raise ModelError(
                    f"refused model base_url {base_url!r}: not on the endpoint "
                    "allowlist (deepseek_base_url + JARVIS_MODEL_BASE_URL_ALLOWLIST)")
            key = self.api_key if _is_deepseek_endpoint(base_url) else "local"
        elif not self.api_key:
            raise ModelError("DEEPSEEK_API_KEY is not set (~/.config/jarvis/env, JARVIS_DEEPSEEK_API_KEY=...)")
        else:
            key = self.api_key
        name = model_name or _model_override or self.transport.name

        final: dict | None = None
        async for ev in self.transport.complete(
                messages, tools=tools, temperature=temperature,
                model_name=name, base_url=base_url, key=key):
            if ev["type"] == "token":
                yield ev
            else:
                final = ev

        assert final is not None
        usage = final["usage"]
        if budget is not None:
            budget.add(usage or {})
        try:
            await record_model_call(conversation_id, name, usage, messages, tools)
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
