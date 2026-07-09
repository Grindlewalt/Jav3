"""The ReAct loop: reason -> tool -> observe -> repeat -> finish.

With an empty tool registry this degenerates to plain chat, but the loop shape
is what M3+ tools plug into. Yields SSE-ready events:
  {"type": "token", "text": ...}          streamed answer text
  {"type": "tool", "name", "args"}        a tool is being called
  {"type": "final", "content": ...}       the finished assistant message
"""
import asyncio
import json
from collections import OrderedDict
from typing import AsyncIterator

import aiosqlite

from ..config import settings
from ..memory import standing_rules_tail
from .budget import BudgetExceeded
from .model import model
from .tools import registry

# Tools that mutate durable state: their results are the model's record of
# what it changed, so eviction never touches them (reads are disposable,
# writes are load-bearing).
WRITE_PINNED = frozenset({"write_file", "edit_file", "journal_update",
                          "memory_write", "git_commit_request"})

# conversation_id -> project paths the model has read (read_file) or written
# (write_file) there — the read-before-edit guard. In-memory and bounded; a
# restart just costs one extra read per file.
_files_seen: OrderedDict[int, set[str]] = OrderedDict()
_FILES_SEEN_MAX_CONVOS = 256


def _note_seen(conversation_id: int, path: str) -> None:
    paths = _files_seen.setdefault(conversation_id, set())
    paths.add(path)
    _files_seen.move_to_end(conversation_id)
    while len(_files_seen) > _FILES_SEEN_MAX_CONVOS:
        _files_seen.popitem(last=False)


def _guard_blind_edit(conversation_id: int, name: str, args: dict) -> str | None:
    """An instructional error instead of dispatching an edit of a file the
    model never read here — prevents whole-class bad edits (stale find text,
    wrong file). write_file is exempt: full overwrites are staged + reviewed."""
    if name != "edit_file":
        return None
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return None
    if path in _files_seen.get(conversation_id, set()):
        return None
    return (f"error: you haven't read '{path}' in this conversation. Call "
            "read_file on it first so 'find' matches the current text, then "
            "retry the edit.")


async def run_turn(
    db: aiosqlite.Connection,
    conversation_id: int,
    system_prompt: str,
    history: list[dict],
    tools: list[dict] | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    self_check: bool = True,
    max_iterations: int | None = None,
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

    n_iter = max_iterations or settings.max_react_iterations
    read_only = registry.read_only_names()   # once per turn; hot-reload can wait
    tool_msgs: list[dict] = []   # {"idx", "round", "name"} per tool result added
    err_streak = 0               # consecutive failed/empty/duplicate results
    force_conclude = False       # dead-end breaker tripped: withdraw tools
    # (name, canonical args) -> tool_msgs entry, for duplicate read-only calls.
    # Cleared whenever a mutating tool runs — state may have changed under it.
    seen_calls: dict[tuple, dict] = {}
    for i in range(n_iter):
        # on the final allowed round — or once the dead-end breaker trips —
        # drop tools so the model must produce an answer from what it has
        # instead of another tool call it can't act on
        call_tools = None if (i == n_iter - 1 or force_conclude) else (tools or None)
        final: dict | None = None
        try:
            async for event in model.complete(
                messages, tools=call_tools, conversation_id=conversation_id,
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

        if call_tools is None:
            # Tools were withheld (final round, or the dead-end breaker) but
            # calls came back anyway — DSML text recovery can do that. Don't
            # execute them: force a text answer from what's been gathered, so
            # the operator gets a real summary instead of a bare "(stopped)".
            # (Convo 31: the news agent burned its cap on good reads, then
            # answered the answer-forcing round with more tool markup and the
            # operator got nothing.)
            conclusion = ""
            try:
                async for ev in model.complete(
                    messages + [{"role": "user", "content":
                        "Your tool budget for this turn is exhausted. Using "
                        "only what you already learned above, give your best "
                        "answer now. Be explicit about anything you could not "
                        "determine."}],
                    conversation_id=conversation_id,
                    model_name=model_name, base_url=base_url,
                ):
                    if ev["type"] == "token":
                        yield ev
                    else:
                        conclusion = ev["content"] or ""
            except Exception:  # noqa: BLE001 — conclusion is best-effort
                conclusion = ""
            if conclusion.strip():
                if rules:
                    conclusion = await _enforce_rules(conclusion, rules)
                yield {"type": "final", "content": conclusion}
            else:
                yield {"type": "final", "content":
                       "(stopped: hit the tool budget for this turn without "
                       "reaching a conclusion — try rephrasing the task or "
                       "point me at where the answer lives)"}
            return

        messages.append({
            "role": "assistant",
            "content": final["content"] or None,
            "tool_calls": final["tool_calls"],
        })
        parsed = []
        for tc in final["tool_calls"]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            parsed.append((tc, name, args))
            yield {"type": "tool", "name": name, "args": args}

        async def _run_one(name: str, args: dict) -> str:
            blocked = _guard_blind_edit(conversation_id, name, args)
            if blocked is not None:
                return blocked
            if name in read_only:
                prev = seen_calls.get((name, json.dumps(args, sort_keys=True)))
                if prev is not None and not prev.get("evicted"):
                    # CC's re-read lesson: point at the earlier result instead
                    # of re-sending the bytes (an evicted result re-dispatches)
                    return (f"duplicate call: you already ran {name} with these "
                            "exact arguments this turn — the result is unchanged, "
                            "see above. Change the arguments or take a different "
                            "approach.")
            result = await registry.dispatch(name, args)
            path = args.get("path")
            if (name in ("read_file", "write_file") and isinstance(path, str)
                    and not result.startswith("error:")):
                _note_seen(conversation_id, path)
            return result

        # a round whose calls are ALL flagged read-only runs them concurrently
        # (three reads cost one round-trip, not three); anything unflagged is
        # assumed to write — fail closed — and keeps the serial path
        if len(parsed) > 1 and all(n in read_only for _, n, _ in parsed):
            results = await asyncio.gather(
                *(_run_one(n, a) for _, n, a in parsed))
        else:
            results = [await _run_one(n, a) for _, n, a in parsed]

        # DB writes + message appends stay sequential and ordered — the single
        # aiosqlite connection must never be used concurrently
        for (tc, name, args), result in zip(parsed, results):
            await db.execute(
                "INSERT INTO tool_calls (conversation_id, tool, args, result) VALUES (?, ?, ?, ?)",
                (conversation_id, name, json.dumps(args), result[:10000]),
            )
            await db.commit()
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": _cap_result(name, result)})
            tool_msgs.append({"idx": len(messages) - 1, "round": i, "name": name})
            if name in read_only:
                seen_calls[(name, json.dumps(args, sort_keys=True))] = tool_msgs[-1]
            else:
                seen_calls.clear()   # a mutating call may invalidate any read
            failed = (not result.strip() or result.startswith(
                ("error:", "no matches", "note:", "duplicate call:")))
            err_streak = err_streak + 1 if failed else 0
        _evict_stale_results(messages, tool_msgs, i)

        # dead-end breaker: a grinding turn gets steered, then stopped —
        # the note rides the last tool result so it's adjacent to the failure
        if err_streak >= settings.dead_end_force_answer:
            force_conclude = True
            messages[-1] = {**messages[-1], "content": messages[-1]["content"] +
                            f"\n\n[system note: {err_streak} consecutive tool "
                            "calls failed or returned nothing — tools are now "
                            "disabled. Summarize what you tried, what failed, "
                            "and what you could not determine. If the thing "
                            "you're looking for may simply not exist, say so.]"}
        elif err_streak >= settings.dead_end_error_streak:
            messages[-1] = {**messages[-1], "content": messages[-1]["content"] +
                            f"\n\n[system note: {err_streak} consecutive tool "
                            "calls failed or returned nothing. Diagnose why "
                            "before retrying: change strategy, delegate "
                            "(research / spawn_agent), or report honestly what "
                            "can't be found. Do not repeat similar calls.]"}

    yield {"type": "final",
           "content": "(stopped: hit the ReAct iteration limit without finishing)"}


def _cap_result(name: str, result: str) -> str:
    """A tool result rides every remaining iteration of the turn, so what
    enters the message list is capped (the DB copy is truncated separately)."""
    cap = settings.tool_result_max_chars
    if len(result) <= cap:
        return result
    return (result[:cap] + f"\n...(truncated: {len(result):,} chars total. "
            f"Re-call {name} with a narrower target if you need the rest.)")


def _evict_stale_results(messages: list[dict], tool_msgs: list[dict],
                         current_round: int) -> None:
    """Replace big tool results from older rounds with a one-line stub. The
    model has already acted on them; re-sending a multi-KB dump every remaining
    iteration costs tokens and pulls attention off the live task. Small results
    stay (cheap, and mutating history invalidates the provider's prefix cache,
    so eviction is reserved for results where the savings clearly win)."""
    horizon = current_round - settings.tool_result_keep_recent
    for t in tool_msgs:
        if t["round"] > horizon or t.get("evicted"):
            continue
        if t["name"] in WRITE_PINNED:
            continue  # the model's record of what it changed — never dropped
        content = messages[t["idx"]]["content"]
        if len(content) <= settings.tool_result_evict_chars:
            continue
        messages[t["idx"]] = {**messages[t["idx"]], "content":
                              f"[{t['name']} result from an earlier step "
                              f"({len(content):,} chars) was dropped to keep "
                              "context small. Call the tool again if you still "
                              "need it.]"}
        t["evicted"] = True


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
