"""The ReAct loop: reason -> tool -> observe -> repeat -> finish.

With an empty tool registry this degenerates to plain chat, but the loop shape
is what M3+ tools plug into. Yields SSE-ready events:
  {"type": "token", "text": ...}          streamed answer text
  {"type": "tool", "name", "args"}        a tool is being called
  {"type": "final", "content": ...}       the finished assistant message
"""
import json
from typing import AsyncIterator

import aiosqlite

from ..config import settings
from ..memory import standing_rules_tail
from .budget import BudgetExceeded
from .model import model
from .tools import registry


async def run_turn(
    db: aiosqlite.Connection,
    conversation_id: int,
    system_prompt: str,
    history: list[dict],
    tools: list[dict] | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    self_check: bool = True,
) -> AsyncIterator[dict]:
    messages: list[dict] = [{"role": "system", "content": system_prompt}, *history]
    if tools is None:
        tools = registry.openai_tool_specs()

    # Standing rules from the operator's memory. Skipped for internal subagents
    # (self_check=False): their output is intermediate and gets synthesized, so
    # enforcing operator formatting on it just burns tokens.
    rules = standing_rules_tail() if self_check else ""

    # Tool schemas pull the model's attention off the system-prompt rules:
    # measured on deepseek-v4-flash, em-dash violations run ~0% with no tools
    # but ~65% once tools are attached. Restating the rules in the latest user
    # turn (closest to generation) roughly halves that to ~33% — it helps but
    # doesn't fully solve it, so the final answer also goes through a no-tools
    # self-check below. Model-only; persisted DB history stays clean.
    if tools and rules:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "user":
                messages[i] = {**messages[i],
                               "content": (messages[i]["content"] or "") + "\n\n" + rules}
                break

    for _ in range(settings.max_react_iterations):
        final: dict | None = None
        try:
            async for event in model.complete(
                messages, tools=tools or None, conversation_id=conversation_id,
                model_name=model_name, base_url=base_url,
            ):
                if event["type"] == "token":
                    yield event
                else:
                    final = event
        except BudgetExceeded as e:
            yield {"type": "final", "content": f"(stopped: {e})"}
            return

        assert final is not None
        if not final["tool_calls"]:
            content = final["content"] or ""
            # Self-check: a no-tools pass reliably obeys the operator's rules
            # (tools are what break adherence), so it cleans up anything the
            # tool-laden turn let slip. General — it checks against whatever
            # rules are in memory, nothing rule-specific is hardcoded. `rules`
            # is already empty when self_check is off, so this no-ops for subagents.
            if rules and content.strip():
                content = await _enforce_rules(content, rules)
            yield {"type": "final", "content": content}
            return

        messages.append({
            "role": "assistant",
            "content": final["content"] or None,
            "tool_calls": final["tool_calls"],
        })
        for tc in final["tool_calls"]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool", "name": name, "args": args}
            result = await registry.dispatch(name, args)
            await db.execute(
                "INSERT INTO tool_calls (conversation_id, tool, args, result) VALUES (?, ?, ?, ?)",
                (conversation_id, name, json.dumps(args), result[:10000]),
            )
            await db.commit()
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

    yield {"type": "final",
           "content": "(stopped: hit the ReAct iteration limit without finishing)"}


async def _enforce_rules(content: str, rules: str) -> str:
    """No-tools verification pass. flash obeys rules ~100% without tool schemas
    attached, so this reliably fixes violations the tool-laden turn let through.
    Preserves meaning and structure; only touches rule breaks. Falls back to the
    original text on any error so a failed check never blocks the reply."""
    prompt = [
        {"role": "system", "content":
            "You are a strict copy editor for another assistant's reply. Rewrite "
            "it so it fully obeys the operator's rules below. Preserve the "
            "meaning, structure, markdown, and every point exactly; change ONLY "
            "what breaks a rule. If it already obeys every rule, return it "
            "verbatim. Output only the reply text, no preamble or explanation."},
        {"role": "user", "content": f"{rules}\n\n---\nReply to check and fix:\n\n{content}"},
    ]
    try:
        revised = ""
        # temperature 0: this is a deterministic editing task, not creative
        async for ev in model.complete(prompt, temperature=0.0):  # no tools -> reliably obeys
            if ev["type"] == "message":
                revised = ev["content"]
        return revised.strip() or content
    except Exception:  # noqa: BLE001 — never let the check block the answer
        return content
