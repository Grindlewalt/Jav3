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
from .model import model
from .tools import registry


async def run_turn(
    db: aiosqlite.Connection,
    conversation_id: int,
    system_prompt: str,
    history: list[dict],
    tools: list[dict] | None = None,
) -> AsyncIterator[dict]:
    messages: list[dict] = [{"role": "system", "content": system_prompt}, *history]
    if tools is None:
        tools = registry.openai_tool_specs()

    # Tool schemas pull the model's attention off the system-prompt rules:
    # measured on deepseek-v4-flash, em-dash violations jump 0/6 -> 4/6 the
    # moment tools are attached. Restating the operator's hard rules in the
    # latest user turn — closest to generation, where tools can't crowd them
    # out — reclaims adherence (back to 0/6). Model-only; DB history stays clean.
    if tools:
        rules = standing_rules_tail()
        if rules:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i]["role"] == "user":
                    messages[i] = {**messages[i],
                                   "content": (messages[i]["content"] or "") + "\n\n" + rules}
                    break

    for _ in range(settings.max_react_iterations):
        final: dict | None = None
        async for event in model.complete(
            messages, tools=tools or None, conversation_id=conversation_id
        ):
            if event["type"] == "token":
                yield event
            else:
                final = event

        assert final is not None
        if not final["tool_calls"]:
            yield {"type": "final", "content": final["content"]}
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
