"""The single model choke point: every LLM call goes through Model.complete,
and the peak-cost gate lives in front of it. The router drops in here later."""
import json
import time
from datetime import datetime, time as dtime
from typing import AsyncIterator

import httpx

from ..config import settings
from . import budget as budget_mod


class PeakPricingConfirmationRequired(Exception):
    """Raised when a call lands inside a peak-pricing window and the user
    hasn't confirmed they want to pay 2x for this conversation recently."""


class ModelError(Exception):
    pass


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


class Model:
    """DeepSeek behind an OpenAI-compatible chat-completions API."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.deepseek_api_key
        self.base_url = settings.deepseek_base_url.rstrip("/")
        self.name = settings.model_name

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        conversation_id: int | None = None,
        temperature: float | None = None,
        model_name: str | None = None,
        base_url: str | None = None,
    ) -> AsyncIterator[dict]:
        """Stream events: {"type": "token", "text": str} per delta, then one
        {"type": "message", "content": str, "tool_calls": list}. Raises
        PeakPricingConfirmationRequired before any network I/O if gated.

        model_name/base_url override the defaults so an agent can run on a
        different model or a local endpoint (e.g. ollama). A custom endpoint
        usually needs no key, so the DeepSeek-key requirement is relaxed there."""
        if conversation_id is not None:
            check_peak_gate(conversation_id)
        budget = budget_mod.active_budget.get()
        if budget is not None and budget.over():
            raise budget_mod.BudgetExceeded(
                f"token budget spent ({budget.summary()})")
        base = (base_url or self.base_url).rstrip("/")
        name = model_name or self.name
        key = self.api_key
        if base_url:                       # custom endpoint (ollama etc.)
            key = self.api_key or "local"  # local servers ignore the auth header
        elif not key:
            raise ModelError("DEEPSEEK_API_KEY is not set (~/.config/jarvis/env, JARVIS_DEEPSEEK_API_KEY=...)")

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

        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        usage: dict | None = None

        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
            async with client.stream(
                "POST",
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    raise ModelError(f"model API {resp.status_code}: {body[:500]}")
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

        if budget is not None:
            budget.add(usage or {})
        yield {
            "type": "message",
            "content": "".join(content_parts),
            "tool_calls": [tool_calls[i] for i in sorted(tool_calls)],
            "usage": usage,
        }


model = Model()
