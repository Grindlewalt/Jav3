"""The single model choke point: every LLM call goes through Model.complete,
and the peak-cost gate lives in front of it. The router drops in here later."""
import json
import time
from datetime import datetime, time as dtime
from typing import AsyncIterator

import httpx

from ..config import settings


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
    ) -> AsyncIterator[dict]:
        """Stream events: {"type": "token", "text": str} per delta, then one
        {"type": "message", "content": str, "tool_calls": list}. Raises
        PeakPricingConfirmationRequired before any network I/O if gated."""
        if conversation_id is not None:
            check_peak_gate(conversation_id)
        if not self.api_key:
            raise ModelError("DEEPSEEK_API_KEY is not set (~/.config/jarvis/env, JARVIS_DEEPSEEK_API_KEY=...)")

        payload: dict = {
            "model": self.name,
            "messages": messages,
            "max_tokens": settings.model_max_tokens,
            "temperature": settings.model_temperature if temperature is None else temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}

        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
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
                    delta = json.loads(data)["choices"][0].get("delta", {})
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

        yield {
            "type": "message",
            "content": "".join(content_parts),
            "tool_calls": [tool_calls[i] for i in sorted(tool_calls)],
        }


model = Model()
