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

from ..config import settings
from ..memory import standing_rules_tail
from .budget import BudgetExceeded
from .model import model
from .tools import registry

# Tools that mutate durable state: their results are the model's record of
# what it changed, so eviction never touches them (reads are disposable,
# writes are load-bearing).
WRITE_PINNED = frozenset({"write_file", "edit_file", "journal_update",
                          "memory_write", "git_commit_request",
                          "create_agent", "schedule_update", "run_code"})

# Delegation tools whose successful results carry a trust note: the observed
# failure mode is the head re-fetching a subagent's sources to "verify" —
# which re-spends every token the delegation saved.
DELEGATION_TOOLS = frozenset({"research", "spawn_agent", "deploy_agents"})

_TRUST_NOTE = ("\n\n[system note: this is a delegated result — trust it and "
               "build on it; do NOT re-fetch or re-verify its sources "
               "yourself. If a specific gap remains, name it and delegate "
               "that gap too, or answer with what you have.]")

# Hand-rolled web gathering: past a threshold of these in one turn, the model
# is told once to hand the remainder to research (the convo-12 failure shape:
# dozens of one-page web_reads where one research call was the right move).
WEB_HANDROLLED = frozenset({"web_search", "web_read", "read_and_summarize"})

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


def _triage_note(tool_names: set[str]) -> str:
    """Round-1 triage: the orchestrate-or-not fork happens on the FIRST model
    call, so the steering has to already be in context — the mid-flight
    delegation nudge below only rescues turns that have gone long. Rides the
    latest user turn (same channel as the operator rules) because tool
    schemas pull attention off the system prompt, where the behavior bank
    says the same thing to little effect."""
    if "todo_update" not in tool_names:
        return ""
    routes = [r for name, r in (
        ("research", "web gathering goes to research in ONE call"),
        ("spawn_agent", "a self-contained subtask goes to spawn_agent"),
        ("deploy_agents",
         "several independent workstreams go to a deploy_agents team"),
    ) if name in tool_names]
    if not routes:
        return ""
    return ("[triage — size the task before your first tool call. A question "
            "you can already answer: answer it, no tools. A small task (up to "
            "~3 steps): just do it. Anything bigger: FIRST write the plan with "
            "todo_update (one item per step), then execute items in order, "
            "checking each off; delegate the heavy items — "
            + "; ".join(routes) + ".]")


def _guard_blind_edit(conversation_id: int, name: str, args: dict) -> str | None:
    """An instructional error instead of dispatching an edit of a file the
    model never read here — prevents whole-class bad edits (stale find text,
    wrong file). write_file is exempt: a full overwrite needs no prior read."""
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


def db_tool_sink(db, conversation_id: int):
    """The standard persistence sink for run_turn: record each tool call to the
    tool_calls table (result truncated for storage). run_turn holds no db handle
    of its own — the caller supplies this, which keeps the loop storage-agnostic.
    This is the host sink; a guest loop passes on_tool_call=None and its host-side
    guest_turn reconstructs the same record from the streamed tool events, so the
    guest never carries a db handle (the VM-inversion seam)."""
    async def sink(name: str, args: dict, result: str) -> None:
        await db.execute(
            "INSERT INTO tool_calls (conversation_id, tool, args, result) "
            "VALUES (?, ?, ?, ?)",
            (conversation_id, name, json.dumps(args), result[:10000]))
        await db.commit()
    return sink


def _assemble_messages(system_prompt: str, history: list[dict],
                       tools: list[dict] | None, self_check: bool,
                       inject_rules: bool = True):
    """Build the turn's message array and the round-1 steering. Returns
    (messages, tools, rules, can_delegate). Tool schemas pull the model's
    attention off the system-prompt rules (measured on deepseek-v4-flash:
    em-dash violations ~0% with no tools, ~65% with tools), so the operator's
    standing rules + the triage note are restated in the latest user turn,
    closest to generation — model-only; persisted DB history stays clean.
    `rules` is empty for internal subagents (self_check=False), whose output is
    intermediate and gets synthesized, so enforcing operator formatting on it
    just burns tokens."""
    messages: list[dict] = [{"role": "system", "content": system_prompt}, *history]
    if tools is None:
        tools = registry.openai_tool_specs()
    rules = standing_rules_tail() if self_check else ""
    tool_names = {t["function"]["name"] for t in (tools or [])}
    can_delegate = bool(tool_names & {"research", "spawn_agent", "deploy_agents"})
    triage = _triage_note(tool_names) if (tools and self_check) else ""
    # inject_rules=False for the voice local tier. The restatement was measured
    # on deepseek-v4-flash against ~30 tool schemas; a 4B with 12 slim schemas
    # and a 6k-char prompt does not lose the system-prompt rules — it instead
    # reads the appended text as something the operator SAID, and answers it
    # ("Got it, sir. No em dashes, and I'll keep the visuals clean." in reply
    # to "That's great."). The rules still ride the system-prompt tail, which
    # assemble_system_prompt never lets anything drop.
    inject = "\n\n".join(x for x in (triage, rules) if x) if inject_rules else ""
    if tools and inject:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "user":
                messages[i] = {**messages[i],
                               "content": (messages[i]["content"] or "") + "\n\n" + inject}
                break
    return messages, tools, rules, can_delegate


async def _force_conclusion(messages: list[dict], conversation_id: int,
                            model_name: str | None, base_url: str | None,
                            rules: str, rewrite_rules: bool = True) -> AsyncIterator[dict]:
    """Tools were withheld (final round or dead-end breaker) but the model still
    emitted calls — DSML text recovery can do that. Don't execute them: nudge for
    a plain-prose answer from what's already gathered, so the operator gets a real
    summary instead of a bare "(stopped)" (convo 31). Yields token events, then
    one final."""
    conclusion = ""
    nudge = ("Your tool budget for this turn is exhausted. Using only "
             "what you already learned above, give your best answer "
             "now. Be explicit about anything you could not determine.")
    # two attempts: a tool-fixated model sometimes answers the first nudge with
    # MORE tool markup (DSML recovery leaves content empty — convo 33), so the
    # retry demands plain prose outright
    for attempt in range(2):
        try:
            async for ev in model.complete(
                messages + [{"role": "user", "content": nudge}],
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
            break
        nudge = ("STOP. No more tool calls — they are disabled and any "
                 "tool syntax is discarded. Reply in PLAIN PROSE only: "
                 "summarize what you found above and what remains "
                 "unknown.")
    if conclusion.strip():
        if rules and rewrite_rules:
            conclusion = await _enforce_rules(conclusion, rules)
        yield {"type": "final", "content": conclusion}
    else:
        yield {"type": "final", "content":
               "(stopped: hit the tool budget for this turn without "
               "reaching a conclusion — try rephrasing the task or "
               "point me at where the answer lives)"}


def _steer(messages: list[dict], i: int, n_iter: int, err_streak: int,
           can_delegate: bool, has_todo: bool = False) -> bool:
    """Mid-flight nudges appended to the last tool result (adjacent to the
    failure). Three axes: a dead-end breaker on consecutive failed/empty
    results (with a one-line course-correct on the FIRST failure, before a
    streak forms), delegation/wrap-up pressure as the round budget runs down,
    and a periodic plan re-check so the next call follows the plan instead of
    free-associating. Returns True if the breaker tripped this round — the
    caller withdraws tools next round."""
    force = False
    noted = False
    if err_streak >= settings.dead_end_force_answer:
        force = noted = True
        messages[-1] = {**messages[-1], "content": messages[-1]["content"] +
                        f"\n\n[system note: {err_streak} consecutive tool "
                        "calls failed or returned nothing — tools are now "
                        "disabled. Summarize what you tried, what failed, "
                        "and what you could not determine. If the thing "
                        "you're looking for may simply not exist, say so.]"}
    elif err_streak >= settings.dead_end_error_streak:
        noted = True
        messages[-1] = {**messages[-1], "content": messages[-1]["content"] +
                        f"\n\n[system note: {err_streak} consecutive tool "
                        "calls failed or returned nothing. Diagnose why "
                        "before retrying: change strategy, delegate "
                        "(research / spawn_agent), or report honestly what "
                        "can't be found. Do not repeat similar calls.]"}
    elif err_streak == 1:
        # first failure of a streak: one cheap line so the next call is a
        # deliberate correction, not a shrug-and-move-on
        noted = True
        messages[-1] = {**messages[-1], "content": messages[-1]["content"] +
                        "\n\n[system note: that call failed or returned "
                        "nothing. Read the message above and make ONE "
                        "deliberate adjustment (path, arguments, or approach) "
                        "toward the same goal — don't repeat the call "
                        "unchanged and don't move on as if it succeeded.]"}

    if i + 1 == settings.delegate_nudge_round and can_delegate:
        messages[-1] = {**messages[-1], "content": messages[-1]["content"] +
                        f"\n\n[system note: {i + 1} tool rounds used of "
                        f"{n_iter}. If substantial gathering or multi-step "
                        "work remains, STOP hand-rolling calls: hand web "
                        "gathering to the research tool in one call, hand "
                        "subtasks to spawn_agent or a deploy_agents team, "
                        "and keep a todo_update "
                        "plan so you execute in a straight line.]"}
    elif i + 1 == (n_iter * 2) // 3:
        messages[-1] = {**messages[-1], "content": messages[-1]["content"] +
                        f"\n\n[system note: {i + 1} of {n_iter} tool rounds "
                        "used — start concluding. Finish the current step, "
                        "then answer with what you have and say plainly "
                        "what you could not determine.]"}
    elif (not noted and has_todo and settings.plan_recheck_every
          and (i + 1) % settings.plan_recheck_every == 0):
        # periodic progress check against the model's own plan; suppressed on
        # rounds that already carry a note so nudges never stack
        messages[-1] = {**messages[-1], "content": messages[-1]["content"] +
                        "\n\n[system note: progress check — against your "
                        "todo plan: mark finished items done (todo_update) "
                        "and make the next call serve the next open item. If "
                        "what you've learned changed the plan, revise it "
                        "first, then continue.]"}
    return force


async def run_turn(
    conversation_id: int,
    system_prompt: str,
    history: list[dict],
    tools: list[dict] | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    self_check: bool = True,
    max_iterations: int | None = None,
    on_tool_call=None,
    rewrite_rules: bool = True,
    inject_rules: bool = True,
) -> AsyncIterator[dict]:
    messages, tools, rules, can_delegate = _assemble_messages(
        system_prompt, history, tools, self_check, inject_rules)

    n_iter = max_iterations or settings.max_react_iterations
    offered = {t["function"]["name"] for t in (tools or [])}
    has_todo = "todo_update" in offered
    has_research = "research" in offered
    web_calls = 0                # hand-rolled web gathering calls this turn
    web_nudged = False
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
            # rewrite_rules=False (voice turns) skips only this second-pass
            # rewrite — the text was already SPOKEN as it streamed, so a
            # post-hoc rewrite would silently diverge from what was heard.
            if rules and content.strip() and rewrite_rules:
                content = await _enforce_rules(content, rules)
            yield {"type": "final", "content": content}
            return

        if call_tools is None:
            # tools withheld but calls came back (DSML recovery) — nudge to a
            # plain-prose answer instead of executing them
            async for ev in _force_conclusion(messages, conversation_id,
                                               model_name, base_url, rules,
                                               rewrite_rules=rewrite_rules):
                yield ev
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
            yield {"type": "tool", "id": tc["id"], "name": name, "args": args}

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
            if on_tool_call is not None:
                await on_tool_call(name, args, result)
            failed = (not result.strip() or result.startswith(
                ("error:", "no matches", "note:", "duplicate call:")))
            content = _cap_result(name, result)
            if name in DELEGATION_TOOLS and not failed:
                content += _TRUST_NOTE
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": content})
            tool_msgs.append({"idx": len(messages) - 1, "round": i, "name": name})
            # the GUI renders live activity rows from this: pair to the tool
            # event by id, mark ok/err, carry the result for click-to-expand
            yield {"type": "tool_result", "id": tc["id"], "name": name,
                   "ok": not result.startswith(("error:", "duplicate call:")),
                   "result": result[:10_000]}
            if name in read_only:
                seen_calls[(name, json.dumps(args, sort_keys=True))] = tool_msgs[-1]
            else:
                seen_calls.clear()   # a mutating call may invalidate any read
            err_streak = err_streak + 1 if failed else 0
            if name in WEB_HANDROLLED:
                web_calls += 1
        _evict_stale_results(messages, tool_msgs, i)

        # mid-flight steering: dead-end breaker + delegation/wrap-up nudges,
        # appended to the last tool result so they sit adjacent to the failure
        if _steer(messages, i, n_iter, err_streak, can_delegate, has_todo):
            force_conclude = True
        if (has_research and not web_nudged
                and web_calls >= settings.web_handroll_nudge > 0):
            web_nudged = True
            messages[-1] = {**messages[-1], "content": messages[-1]["content"] +
                            f"\n\n[system note: {web_calls} hand-rolled web "
                            "calls this turn. If more gathering remains, hand "
                            "the remainder to the research tool in ONE call "
                            "and continue from its report instead of reading "
                            "pages yourself.]"}

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
