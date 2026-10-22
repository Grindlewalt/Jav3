"""The ReAct loop: reason -> tool -> observe -> repeat -> finish.

With an empty tool registry this degenerates to plain chat, but the loop shape
is what M3+ tools plug into. Yields SSE-ready events:
  {"type": "token", "text": ...}          streamed answer text
  {"type": "tool", "name", "args"}        a tool is being called
  {"type": "final", "content": ...}       the finished assistant message
"""
import asyncio
import base64
import json
import posixpath
import re
import shlex
from collections import OrderedDict
from typing import AsyncIterator

from ..config import settings
from ..memory import standing_rules_tail
from . import imageresult
from .budget import BudgetExceeded
from .model import model
from .tools import registry, toolsections

# Tools that mutate durable state: their results are the model's record of
# what it changed, so eviction never touches them (reads are disposable,
# writes are load-bearing).
WRITE_PINNED = frozenset({"write_file", "edit_file", "journal_update",
                          "memory_write", "git_commit_request", "git_push_request",
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

# conversation_id -> project paths the model has read (read_file, or a plain
# cat/sed -n/head/tail/nl in run_code) or written (write_file) there — the
# read-before-edit guard. In-memory and bounded; a restart just costs one extra
# read per file.
_files_seen: OrderedDict[int, set[str]] = OrderedDict()
_FILES_SEEN_MAX_CONVOS = 256


def _note_seen(conversation_id: int, path: str) -> None:
    paths = _files_seen.setdefault(conversation_id, set())
    paths.add(path)
    paths.add(posixpath.normpath(path))     # ./a.py, a//b.py and a.py are one file
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


async def _drain_inbox() -> str:
    """Anything another agent addressed to this turn, or "".

    An inbox has to be PULLED. The host cannot reach into a running guest — the
    gateway serves four ops and every one of them is guest-initiated — so the
    loop asks, on the connection it already has, at the one moment where new
    input can be added without corrupting an in-flight model call: between
    iterations. `inbox_fetch` is a tool folder purely so this crosses
    `broker_dispatch`, where the op_id pinning that proves who is asking lives.

    Swallowing the error is safe up to the claim: a dispatch that fails before
    or during the claim's transaction leaves the message in the inbox and the
    next round tries again. It is NOT safe after — a reply lost on the way back
    has already consumed the row, and only the transcript copy the claim writes
    rescues it (and only for a turn that has a next turn). See agentmsg.claim.
    Swallowing is still right here: a mail check must not end a turn doing
    unrelated work, and raising would not un-consume anything."""
    try:
        note = await registry.dispatch("inbox_fetch", {})
    except Exception:  # noqa: BLE001 — a mail check must never end a turn
        return ""
    note = (note or "").strip()
    return "" if note.startswith("error:") else note


# What counts as reading a file in run_code: the command visibly prints it.
_READERS = frozenset({"cat", "head", "tail", "less", "more", "nl", "sed"})
_SHOWS_INPUT = frozenset({"cat", "head", "tail", "less", "more", "nl", "sed", "tee"})
_SEPARATORS = frozenset(";&|()")
_REDIRECT = frozenset("<>&")
_MIN_FIND_SEEN = 30      # a `find` this long that a tool already returned is a read


def _rel_project_path(arg: str) -> str:
    """A path argument as the project-relative path edit_file would be given."""
    root = str(settings.projects_dir).rstrip("/") + "/"
    if arg.startswith(root):
        arg = arg[len(root):].partition("/")[2]        # drop the slug directory
    return posixpath.normpath(arg) if arg else arg


def _paths_read_by(command: str) -> set[str]:
    """Project paths a shell command visibly reads, by a conservative parse: a
    segment of `cat`, `head`, `tail`, `less`, `more`, `nl` or `sed -n` that names
    the file and whose output is not redirected into a file or piped into
    something that hides it (`| wc -l`). Benchmark game 2026-10-01: the agent had
    `cat`/`sed`-read a file in run_code and edit_file refused it as unread. Globs,
    variables and anything the parse cannot follow count as no read."""
    found: set[str] = set()
    for line in (command or "").splitlines():
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            toks = list(lex)
        except ValueError:                  # an unclosed quote, or a heredoc body
            continue
        segs: list[tuple[list[str], str]] = []
        cur: list[str] = []
        for t in toks:
            if t and set(t) <= _SEPARATORS:
                segs.append((cur, t))
                cur = []
            else:
                cur.append(t)
        segs.append((cur, ""))
        moved = False                       # after a `cd`, a relative path is another file
        for k, (seg, sep) in enumerate(segs):
            if seg and seg[0] in ("cd", "pushd", "popd"):
                moved = True
            if moved or not seg or posixpath.basename(seg[0]) not in _READERS:
                continue
            cmd, args = posixpath.basename(seg[0]), seg[1:]
            if cmd == "sed":
                flags = [a for a in args if a.startswith("-") and not a.startswith("--")]
                quiet = any("n" in a for a in flags) or "--quiet" in args or "--silent" in args
                in_place = any("i" in a for a in flags) or any(
                    a.startswith("--in-place") for a in args)
                if not quiet or in_place:
                    continue
            if sep in ("|", "|&"):          # piped on: only into something that shows it
                nxt = segs[k + 1][0] if k + 1 < len(segs) else []
                if not nxt or posixpath.basename(nxt[0]) not in _SHOWS_INPUT:
                    continue
            paths: list[str] = []
            hidden = False
            i = 0
            while i < len(args):
                a = args[i]
                if a and set(a) <= _REDIRECT:
                    # a redirect: `> f` hides the output, `2> f`, `2>&1` and `<<EOF`
                    # do not, and a target is never a path being read (`< f` is)
                    if ">" in a and a != ">&" and (i == 0 or args[i - 1] != "2"):
                        hidden = True
                    i += 1 if a == "<" else 2
                    continue
                if not a.startswith("-"):
                    paths.append(a)
                i += 1
            if not hidden:
                found.update(_rel_project_path(a) for a in paths)
    return found


def _guard_blind_edit(conversation_id: int, name: str, args: dict,
                      messages: list[dict] | None = None) -> str | None:
    """An instructional error instead of dispatching an edit of a file the
    model never read here — prevents whole-class bad edits (stale find text,
    wrong file). write_file is exempt: a full overwrite needs no prior read.
    A read is read_file, a run_code that cat/sed -n/head/tail'd the exact path
    (see _paths_read_by), or a `find` text a tool already returned this turn
    (edit_file itself still demands it match the file exactly)."""
    if name != "edit_file":
        return None
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return None
    seen = _files_seen.get(conversation_id, set())
    if path in seen or posixpath.normpath(path) in seen:
        return None
    find = args.get("find")
    if (messages and isinstance(find, str) and len(find.strip()) >= _MIN_FIND_SEEN
            and any(m.get("role") == "tool" and isinstance(m.get("content"), str)
                    and find in m["content"] for m in messages)):
        return None
    return (f"error: you haven't read '{path}' in this conversation. Call "
            "read_file on it first so 'find' matches the current text, then "
            "retry the edit. (A cat, sed -n, head or tail of that exact path in "
            "run_code counts as a read too.)")


_LEADING_SLEEP = re.compile(
    r"^\s*(?:sleep\s+(\d+)|(?:import\s+time\s*[;\n]\s*)?time\.sleep\(\s*(\d+))", re.I)
MAX_ORCH_SLEEP = 60


def _guard_orchestrator_sleep(name: str, args: dict, offered) -> str | None:
    """An orchestrator (the turn holds plan_status) that starts a run_code call
    with a long sleep is polling the wrong way: conv 500 spent ~4,000 s in 16
    'sleep 240-285' calls, one killed at run_code's 300 s limit, and none of
    them could see the plan or wake for a message. plan_status(wait_seconds)
    does both."""
    if name != "run_code" or "plan_status" not in offered:
        return None
    m = _LEADING_SLEEP.match(str(args.get("command") or args.get("code") or ""))
    secs = int(next((g for g in (m.groups() if m else ()) if g), 0))
    if secs <= MAX_ORCH_SLEEP:
        return None
    return (f"error: don't wait with run_code (sleep {secs}): it blocks a round, dies at "
            "300 s and cannot see the plan. Call plan_status with wait_seconds (up to "
            "600); it returns as soon as an item changes state or a message arrives "
            "for you. To check files, run the check itself without the sleep.")


def db_tool_sink(db, conversation_id: int):
    """The standard persistence sink for run_turn: record each tool call to the
    tool_calls table (result truncated for storage). run_turn holds no db handle
    of its own — the caller supplies this, which keeps the loop storage-agnostic.
    This is the host sink; a guest loop passes on_tool_call=None and its host-side
    guest_turn reconstructs the same record from the streamed tool events, so the
    guest never carries a db handle (the VM-inversion seam)."""
    async def sink(name: str, args: dict, result: str, call_id: str | None = None) -> None:
        # the model's id for the call rides along (the loop sets it around this
        # sink; the guest path passes it): a security event names its step by it
        cv = getattr(registry, "call_id", None)
        cid = call_id or (cv.get() if cv is not None else None)
        await db.execute(
            "INSERT INTO tool_calls (conversation_id, tool, args, result, call_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, name, json.dumps(args), result[:10000],
             str(cid)[:80] if cid else None))
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
                content = messages[i]["content"]
                if isinstance(content, list):
                    # a multimodal message (a screenshot re-attached as the
                    # latest "user" turn): add the note as one more text part
                    content = content + [{"type": "text", "text": inject}]
                else:
                    content = (content or "") + "\n\n" + inject
                messages[i] = {**messages[i], "content": content}
                break
    return messages, tools, rules, can_delegate


# DeepSeek tool-call markup left in a reply's text (the gateway's DSML recovery
# could not parse it). Here, not in model.py: the guest ships this loop with its
# own thin model shim. '｜' is U+FF5C.
_TOOL_MARKUP = re.compile(r"<｜+\s*DSML\s*｜+")

# A reply the output cap cuts off (finish_reason length) is asked to continue, or
# for a smaller tool call, this many times a turn before the cut text is returned.
_MAX_CUTS = 2
_CUT_NOTE = "\n\n(the reply was cut off at the model's output limit)"


def has_tool_markup(content: str) -> bool:
    return bool(content) and _TOOL_MARKUP.search(content) is not None

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
                if ev["type"] in ("token", "retry"):
                    yield ev
                else:
                    conclusion = ev["content"] or ""
                    if conclusion.strip() and ev.get("finish_reason") == "length":
                        conclusion += _CUT_NOTE
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
            conclusion = await _enforce_rules(conclusion, rules, conversation_id)
        yield {"type": "final", "content": conclusion}
    else:
        yield {"type": "final", "content":
               "(stopped: hit the tool budget for this turn without "
               "reaching a conclusion — try rephrasing the task or "
               "point me at where the answer lives)"}


def _note(messages: list[dict], text: str) -> None:
    """Append a system note to the latest TOOL result (next to the call it is
    about). A screenshot's user message can follow that result, and its content
    is a list: appending a str to messages[-1] there crashed the turn with
    "can only concatenate list (not "str") to list" (2026-09-27)."""
    k = next((j for j in range(len(messages) - 1, -1, -1)
              if messages[j].get("role") == "tool"), len(messages) - 1)
    content = messages[k].get("content")
    if isinstance(content, list):
        content = content + [{"type": "text", "text": text.strip()}]
    else:
        content = (content or "") + text
    messages[k] = {**messages[k], "content": content}


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
        _note(messages,
            f"\n\n[system note: {err_streak} consecutive tool "
            "calls failed or returned nothing — tools are now "
            "disabled. Summarize what you tried, what failed, "
            "and what you could not determine. If the thing "
            "you're looking for may simply not exist, say so.]")
    elif err_streak >= settings.dead_end_error_streak:
        noted = True
        _note(messages,
            f"\n\n[system note: {err_streak} consecutive tool "
            "calls failed or returned nothing. Diagnose why "
            "before retrying: change strategy, delegate "
            "(research / spawn_agent), or report honestly what "
            "can't be found. Do not repeat similar calls.]")
    elif err_streak == 1:
        # first failure of a streak: one cheap line so the next call is a
        # deliberate correction, not a shrug-and-move-on
        noted = True
        _note(messages,
            "\n\n[system note: that call failed or returned "
            "nothing. Read the message above and make ONE "
            "deliberate adjustment (path, arguments, or approach) "
            "toward the same goal — don't repeat the call "
            "unchanged and don't move on as if it succeeded.]")

    if i + 1 == settings.delegate_nudge_round and can_delegate:
        _note(messages,
            f"\n\n[system note: {i + 1} tool rounds used of "
            f"{n_iter}. If substantial gathering or multi-step "
            "work remains, STOP hand-rolling calls: hand web "
            "gathering to the research tool in one call, hand "
            "subtasks to spawn_agent or a deploy_agents team, "
            "and keep a todo_update "
            "plan so you execute in a straight line.]")
    elif i + 1 == (n_iter * 2) // 3:
        _note(messages,
            f"\n\n[system note: {i + 1} of {n_iter} tool rounds "
            "used — start concluding. Finish the current step, "
            "then answer with what you have and say plainly "
            "what you could not determine. If you owe a plan_report, "
            "file it before the rounds run out (status failed with the "
            "next step if the item is unfinished).]")
    elif (not noted and has_todo and settings.plan_recheck_every
          and (i + 1) % settings.plan_recheck_every == 0):
        # periodic progress check against the model's own plan; suppressed on
        # rounds that already carry a note so nudges never stack
        _note(messages,
            "\n\n[system note: progress check — against your "
            "todo plan: mark finished items done (todo_update) "
            "and make the next call serve the next open item. If "
            "what you've learned changed the plan, revise it "
            "first, then continue.]")
    return force


# The one "rounds left" note: a run with a round cap is told once, when it has used
# this share of the cap, that it must write down where it is and report. Benchmark
# game 2026-10-01: three attempts ran into the cap with the work done and never
# reported (16M input tokens wasted); the only earlier note, at two thirds, was
# generic and every run read it as "keep going".
ROUNDS_LOW_AT = 0.8


def _rounds_low_round(n_iter: int) -> int | None:
    """How many rounds must have been used before the rounds-left note rides the
    next tool result: ROUNDS_LOW_AT of the cap, but never the round of the
    two-thirds note (they would stack), and never the last round (it has no tools,
    so no tool result carries a note). None: a cap too small to fit both."""
    at = max(int(n_iter * ROUNDS_LOW_AT), (n_iter * 2) // 3 + 1)
    return at if at <= n_iter - 1 else None


def _rounds_low_note(left: int, plan_item: bool) -> str:
    """The note itself. A plan item (it holds plan_report) writes its progress to
    reports/notes/<item id>.md, the file the retry brief reads (plan.py) when the
    attempt does not finish; any other run is told to wrap up."""
    if plan_item:
        return (f"\n\n[system note: {left} rounds left: write your progress and next "
                "steps to reports/notes/<item id>.md now (<item id> is your plan "
                "item's id) and call plan_report before the cap, status failed with "
                "the next step if the item is unfinished.]")
    return (f"\n\n[system note: {left} rounds left: finish and report what you "
            "have, and say plainly what you could not determine.]")


def new_stats() -> dict:
    """What one turn did that the operator cannot otherwise see (RUNS-08): the
    loop's own recoveries and cut-offs, counted per turn."""
    return {"rounds": 0, "dsml_recovered": 0, "markup_retries": 0,
            "forced_conclusion": 0, "cap_hit": 0, "evictions": 0, "rereads": 0,
            "stop": "final"}


async def run_turn(*args, **kwargs) -> AsyncIterator[dict]:
    """Run one turn (see _run_turn). Yields token / tool / tool_result events,
    then ONE {"type": "turn_stats", ...} (the counters of new_stats() plus the
    stop reason) immediately before the final event, so a caller that ends on
    the final event is unchanged; guest_turn records the row and does not pass
    it on."""
    stats = new_stats()
    async for ev in _run_turn(*args, stats=stats, **kwargs):
        if ev["type"] == "final":
            if ev.get("stop"):
                stats["stop"] = ev["stop"]
            yield {"type": "turn_stats", **stats}
        yield ev


async def _run_turn(
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
    inbox: bool = False,
    stats: dict | None = None,
) -> AsyncIterator[dict]:
    stats = stats if stats is not None else new_stats()
    # Messages other agents addressed to this one (WP5). The first drain happens
    # BEFORE the sandwich is assembled so anything waiting joins `history` and
    # the standing-rules restatement still lands on the last user turn — append
    # it afterwards and the rules end up a message early, which is the exact
    # salience the restatement exists to buy.
    waiting = await _drain_inbox() if inbox else ""
    if waiting:
        history = [*history, {"role": "user", "content": waiting}]
    messages, tools, rules, can_delegate = _assemble_messages(
        system_prompt, history, tools, self_check, inject_rules)
    if waiting:
        yield {"type": "inbox", "text": waiting}
    # what the model is SHOWN: the core tools, the `tools` meta-tool and any
    # loaded section (toolsections.py). `tools` stays the granted set; a call
    # still dispatches under its real name, so no gate sees anything new.
    view = toolsections.View(tools, history)
    guides = view.start_guides()
    if guides:
        messages[0] = {**messages[0], "content": f"{messages[0]['content']}\n\n{guides}"}

    n_iter = max_iterations or settings.max_react_iterations
    offered = view.granted()
    has_todo = "todo_update" in offered
    has_research = "research" in offered
    web_calls = 0                # hand-rolled web gathering calls this turn
    web_nudged = False
    read_only = registry.read_only_names()   # once per turn; hot-reload can wait
    tool_msgs: list[dict] = []   # {"idx", "round", "name"} per tool result added
    image_msgs: list[dict] = []  # {"idx", "round"} per screenshot user-message added
    err_streak = 0               # consecutive failed/empty/duplicate results
    force_conclude = False       # dead-end breaker tripped: withdraw tools
    # (name, canonical args) -> tool_msgs entry, for duplicate read-only calls.
    # Cleared whenever a mutating tool runs — state may have changed under it.
    seen_calls: dict[tuple, dict] = {}
    markup_retries = 0           # tool-call markup that arrived as unparsed text
    cuts = 0                     # rounds the output cap cut off (finish_reason length)
    carry: list[str] = []        # the cut-off text of the answer being continued
    truncated = False            # the answer is still cut off after the continues
    edited: dict[str, int] = {}  # project path -> round of its last edit/write
    low_at = _rounds_low_round(n_iter)
    rounds_nudged = False        # the one rounds-left note has ridden a result
    evicted_spans: list[tuple] = []   # (path, first line, last line) of dropped reads
    for i in range(n_iter):
        # mail check. i == 0 was drained into `history` above; from here a
        # message arriving mid-turn becomes its own user message, so it reads as
        # something that happened DURING the work rather than part of the brief.
        if inbox and i:
            waiting = await _drain_inbox()
            if waiting:
                messages.append({"role": "user", "content": waiting})
                yield {"type": "inbox", "text": waiting}
        # on the final allowed round — or once the dead-end breaker trips —
        # drop tools so the model must produce an answer from what it has
        # instead of another tool call it can't act on
        call_tools = None if (i == n_iter - 1 or force_conclude) else (view.wire() or None)
        # ...except a run that owes a plan_report keeps THAT one tool: withholding
        # it failed 30 of 39 plan attempts with "plan_report was not called"
        # after the work was done (benchmark-game, 2026-09-27)
        report_only = False
        if call_tools is None:
            report = [t for t in (view.wire() or [])
                      if (t.get("function") or {}).get("name") == "plan_report"]
            if report:
                call_tools, report_only = report, True
        final: dict | None = None
        try:
            async for event in model.complete(
                messages, tools=call_tools, conversation_id=conversation_id,
                model_name=model_name, base_url=base_url,
            ):
                if event["type"] in ("token", "retry"):
                    yield event       # retry: the stream dropped, the text so far is void
                else:
                    final = event
        except BudgetExceeded as e:
            # "stop" names why the turn ended without an answer, for a caller
            # that must not mistake it for a failed attempt (plan._settle)
            yield {"type": "final", "content": f"(stopped: {e})", "stop": "budget"}
            return

        assert final is not None
        stats["rounds"] += 1
        # the gateway's DSML recovery names the calls it rebuilt dsml_N
        if any(str(tc.get("id", "")).startswith("dsml_")
               for tc in final["tool_calls"] or ()):
            stats["dsml_recovered"] += 1
        # a reply the output cap cut off is not an answer, and its last tool
        # call is cut mid-argument: nothing from a cut round runs
        cut = final.get("finish_reason") == "length" and bool(
            final["tool_calls"] or (final["content"] or "").strip())
        if cut and final["tool_calls"]:
            if i < n_iter - 1 and cuts < _MAX_CUTS:
                cuts += 1
                messages.append({"role": "assistant",
                                 "content": final["content"] or "(cut off at the output limit)"})
                messages.append({"role": "user", "content": (
                    "Harness note: your last reply hit the output limit in the "
                    "middle of a tool call, so nothing ran. Make the call again "
                    "with less in it (write a large file in several smaller parts).")})
                continue
            yield {"type": "final", "content": "".join(carry) + (final["content"] or "")
                   + _CUT_NOTE}
            return
        if cut and not has_tool_markup(final["content"] or ""):
            if i < n_iter - 1 and cuts < _MAX_CUTS:
                cuts += 1
                carry.append(final["content"])
                messages.append({"role": "assistant", "content": final["content"]})
                messages.append({"role": "user", "content": (
                    "Harness note: your last reply was cut off at the output "
                    "limit. Continue from exactly where it stopped; do not "
                    "repeat what you already wrote.")})
                continue
            truncated = True
        if not final["tool_calls"]:
            content = "".join(carry) + (final["content"] or "")
            # tool-call markup the gateway could not parse is a harness fault,
            # not an answer: ending the turn on it voided every item of a plan
            # run (plan_report never ran). Ask for the call again, twice at most.
            if call_tools and markup_retries < 2 and has_tool_markup(content):
                markup_retries += 1
                stats["markup_retries"] = markup_retries
                messages.append({"role": "assistant", "content": final["content"] or ""})
                messages.append({"role": "user", "content": (
                    "Harness note: your last reply contained tool-call markup "
                    "as plain text, so nothing ran. Make the call again through "
                    "the tool-calling interface, not as text.")})
                continue
            # Self-check: a no-tools pass reliably obeys the operator's rules
            # (tools are what break adherence), so it cleans up anything the
            # tool-laden turn let slip. General — it checks against whatever
            # rules are in memory, nothing rule-specific is hardcoded. `rules`
            # is already empty when self_check is off, so this no-ops for subagents.
            # rewrite_rules=False (voice turns) skips only this second-pass
            # rewrite — the text was already SPOKEN as it streamed, so a
            # post-hoc rewrite would silently diverge from what was heard.
            if rules and content.strip() and rewrite_rules:
                content = await _enforce_rules(content, rules, conversation_id)
            if force_conclude:
                stats["stop"] = "dead_end"       # the breaker withdrew the tools
            elif i == n_iter - 1:
                stats["cap_hit"], stats["stop"] = 1, "cap"   # answered on the last round
            yield {"type": "final", "content": content + (_CUT_NOTE if truncated else "")}
            return

        if call_tools is None or (report_only and any(
                tc["function"]["name"] != "plan_report" for tc in final["tool_calls"])):
            # tools withheld but calls came back (DSML recovery) — nudge to a
            # plain-prose answer instead of executing them
            stats["forced_conclusion"] += 1
            stats["stop"] = "dead_end" if force_conclude else "cap"
            stats["cap_hit"] = int(not force_conclude)
            async for ev in _force_conclusion(messages, conversation_id,
                                               model_name, base_url, rules,
                                               rewrite_rules=rewrite_rules):
                yield ev
            return

        turn = {
            "role": "assistant",
            "content": final["content"] or None,
            "tool_calls": final["tool_calls"],
        }
        # a provider's opaque replay state (Anthropic thinking signatures,
        # Gemini thought signatures) must come back verbatim next iteration
        if final.get("provider_blocks"):
            turn["provider_blocks"] = final["provider_blocks"]
        messages.append(turn)
        carry.clear()
        cuts = 0
        parsed = []
        bad_json: dict[int, str] = {}    # id(tool call) -> why its arguments were refused
        # per call: (note for its result, error that replaces its dispatch),
        # from mapping what the model called onto the real tool
        mapped: dict[int, tuple[str, str | None]] = {}
        for tc in final["tool_calls"]:
            name = tc["function"]["name"]
            raw_args = tc["function"]["arguments"] or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError as e:
                # not JSON at all (an unescaped quote or newline in a string,
                # a cut-off call): never dispatched as an empty call, which
                # would tell the model it forgot arguments it did send. It is
                # told what was wrong and sees the start of what it wrote.
                shown = raw_args if len(raw_args) <= 200 else raw_args[:200] + "..."
                bad_json[id(tc)] = (
                    f"error: the arguments you sent for {name} were not valid JSON "
                    f"({e.msg}, at character {e.pos}), so nothing ran. They began: "
                    f"{shown}\nEscape quotes and newlines inside string values "
                    f"(\\\" and \\n) and call {name} again.")
                parsed.append((tc, name, {"_invalid_json": raw_args[:2000]}))
                yield {"type": "tool", "id": tc["id"], "name": name, "args": parsed[-1][2]}
                continue
            if not isinstance(args, dict):
                # valid JSON but not an object ([..], null, "x"): an empty call,
                # so argcheck names the missing arguments and the model retries,
                # instead of a TypeError ending the whole turn
                args = {}
            if not view.is_meta(name):
                # a merged tool's action -> its real tool; an unloaded section
                # loads; a name outside the granted set never dispatches
                name, args, note, err = view.resolve(name, args)
                mapped[id(tc)] = (note, err)
            parsed.append((tc, name, args))
            yield {"type": "tool", "id": tc["id"], "name": name, "args": args}

        async def _run_one(name: str, args: dict, call_id=None, tc=None) -> str:
            if id(tc) in bad_json:
                return bad_json[id(tc)]
            if view.is_meta(name):
                return view.meta_call(args)
            note, err = mapped.get(id(tc), ("", None))
            if err is not None:
                return err
            result = await _dispatch_one(name, args, call_id)
            return result + note if note and isinstance(result, str) else result

        async def _dispatch_one(name: str, args: dict, call_id=None) -> str:
            blocked = (_guard_blind_edit(conversation_id, name, args, messages)
                       or _guard_orchestrator_sleep(name, args, offered))
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
            if name == "read_file" and evicted_spans:
                # a read of lines this turn already read and then dropped: the
                # cost of evicting (RUNS-03), counted so it can be tuned
                path, (lo, hi) = args.get("path"), _read_span(args)
                if any(p == path and lo <= b and a <= hi for p, (a, b) in evicted_spans):
                    stats["rereads"] += 1
            # the call's id rides along to the broker (registry.call_id) so a
            # host tool can name it the way the tool events do. getattr: a
            # test may stand a bare module in for the registry
            cv = getattr(registry, "call_id", None)
            tok = cv.set(call_id) if cv is not None else None
            try:
                result = await registry.dispatch(name, args)
            finally:
                if tok is not None:
                    cv.reset(tok)
            path = args.get("path")
            if (name in ("read_file", "write_file") and isinstance(path, str)
                    and not result.startswith("error:")):
                _note_seen(conversation_id, path)
            elif (name == "run_code" and isinstance(args.get("command"), str)
                    and result.startswith("exit 0")):
                # `cat src/a.py`, `sed -n 1,80p src/a.py`: the text came back,
                # which is all the guard wants from a read
                for seen_path in _paths_read_by(args["command"]):
                    _note_seen(conversation_id, seen_path)
            return result

        # a round whose calls are ALL flagged read-only runs them concurrently
        # (three reads cost one round-trip, not three); anything unflagged is
        # assumed to write — fail closed — and keeps the serial path
        if len(parsed) > 1 and all(n in read_only or view.is_meta(n)
                                   for _, n, _ in parsed):
            results = await asyncio.gather(
                *(_run_one(n, a, tc.get("id"), tc) for tc, n, a in parsed))
        else:
            results = [await _run_one(n, a, tc.get("id"), tc) for tc, n, a in parsed]

        # DB writes + message appends stay sequential and ordered — the single
        # aiosqlite connection must never be used concurrently
        pending_images: list = []   # imageresult.Image per image a tool returned
        for (tc, name, args), result in zip(parsed, results):
            # peel any screenshot off the result BEFORE persisting/capping, so
            # the ledger and the tool message stay text-only (bytes ride a
            # following user message instead)
            result, img = imageresult.split(result)
            if on_tool_call is not None:
                # the tool_calls sink stores the model's id with the row
                cv = getattr(registry, "call_id", None)
                ctok = cv.set(tc.get("id")) if cv is not None else None
                try:
                    await on_tool_call(name, args, result)
                finally:
                    if ctok is not None:
                        cv.reset(ctok)
            failed = (not result.strip() or result.startswith(
                ("error:", "no matches", "note:", "duplicate call:")))
            content = _cap_result(name, result)
            if name in DELEGATION_TOOLS and not failed:
                content += _TRUST_NOTE
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": content})
            entry = {"idx": len(messages) - 1, "round": i, "name": name}
            path = args.get("path")
            if isinstance(path, str) and path:
                entry["path"] = path
                if name == "read_file":
                    entry["span"] = _read_span(args)
                elif name in ("edit_file", "write_file") and not failed:
                    edited[path] = i     # reads of it before this are now stale
            tool_msgs.append(entry)
            if img is not None and not failed:
                pending_images.append(img)
            # the GUI renders live activity rows from this: pair to the tool
            # event by id, mark ok/err, carry the result for click-to-expand
            yield {"type": "tool_result", "id": tc["id"], "name": name,
                   "ok": not result.startswith(("error:", "duplicate call:")),
                   "result": result[:10_000]}
            if view.is_meta(name):
                continue             # loading tools changes nothing out there
            if name in read_only:
                seen_calls[(name, json.dumps(args, sort_keys=True))] = tool_msgs[-1]
            elif mapped.get(id(tc), ("", None))[1] is None:
                seen_calls.clear()   # a mutating call may invalidate any read
            err_streak = err_streak + 1 if failed else 0
            if name in WEB_HANDROLLED:
                web_calls += 1
        # attach screenshots so the model can SEE them — DeepSeek accepts image
        # content only in a user message, never a tool one, so each rides its own
        for img in pending_images:
            msg = _image_message(img)
            if msg is not None:
                messages.append(msg)
                image_msgs.append({"idx": len(messages) - 1, "round": i})
        if report_only:
            # the last round's only tool was plan_report: it has run, so the
            # turn ends here with whatever the model said alongside it
            stats["cap_hit"], stats["stop"] = 1, "cap"
            yield {"type": "final", "content": (final["content"] or "").strip()
                   or "(filed the plan report at the round limit)"}
            return
        for t in _evict_stale_results(messages, tool_msgs, i, edited):
            stats["evictions"] += 1
            if t["name"] == "read_file" and "path" in t:
                evicted_spans.append((t["path"], t["span"]))
        _evict_stale_images(messages, image_msgs)

        # mid-flight steering: dead-end breaker + delegation/wrap-up nudges,
        # appended to the last tool result so they sit adjacent to the failure
        if _steer(messages, i, n_iter, err_streak, can_delegate, has_todo):
            force_conclude = True
        if (not rounds_nudged and not force_conclude
                and low_at is not None and i + 1 >= low_at):
            rounds_nudged = True
            _note(messages, _rounds_low_note(n_iter - (i + 1), "plan_report" in offered))
        if (has_research and not web_nudged
                and web_calls >= settings.web_handroll_nudge > 0):
            web_nudged = True
            _note(messages,
                f"\n\n[system note: {web_calls} hand-rolled web "
                "calls this turn. If more gathering remains, hand "
                "the remainder to the research tool in ONE call "
                "and continue from its report instead of reading "
                "pages yourself.]")

    stats["cap_hit"], stats["stop"] = 1, "cap"
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


_IMG_CAP = 4_500_000     # ~4.5MB ceiling; DeepSeek rejects oversized images

_DEFAULT_CAPTION = ("[screenshot — act on what you SEE here; coordinates are "
                    "pixels from the top-left of this image]")


def _image_message(img) -> dict | None:
    """A user message carrying a tool's image as an image block. `img` is an
    imageresult.Image (or a bare path). Returns None on a missing, unreadable,
    oversized or non-image payload so a bad render never breaks the turn. The
    mime comes from the bytes, not from whoever labelled them, and the caption
    is the tool's own when it gave one — a desk screenshot is not "the current
    page"."""
    if isinstance(img, str):
        img = imageresult.Image(path=img)
    data = img.data(_IMG_CAP)
    mime = imageresult.sniff(data) if data else None
    if mime is None:
        return None
    b64 = base64.b64encode(data).decode()
    caption = img.caption.strip() if img.caption and img.caption.strip() else None
    return {"role": "user", "content": [
        {"type": "text", "text": f"[{caption}]" if caption else _DEFAULT_CAPTION},
        {"type": "image_url",
         "image_url": {"url": f"data:{mime};base64,{b64}"}}]}


def _evict_stale_images(messages: list[dict], image_msgs: list[dict]) -> None:
    """Keep only the most recent `screenshot_keep_recent` screenshots as real
    image blocks; replace older ones with a text stub. A screenshot is ~1k+
    tokens re-sent every iteration, so stale ones are the biggest avoidable cost
    of a browsing loop — the model only needs the CURRENT view."""
    keep = settings.screenshot_keep_recent
    live = [m for m in image_msgs if not m.get("evicted")]
    stale = live if keep <= 0 else live[:-keep]
    for m in stale:
        messages[m["idx"]] = {"role": "user", "content":
                              "[an earlier screenshot was dropped to keep "
                              "context small; take another screenshot if you "
                              "need to see that view again]"}
        m["evicted"] = True


def _read_span(args: dict) -> tuple[int, float]:
    """(first, last) line a read_file call asked for; a whole-file read is
    (1, inf)."""
    def num(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    start = max(1, num(args.get("offset"), 1))
    limit = num(args.get("limit"), 0)
    return start, (start + limit - 1 if limit > 0 else float("inf"))


# a symbol line in the usual languages: def/class/function/... NAME, a
# `const NAME = (...) =>`/function, or an indented JS/TS method header
_SYMBOL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:pub\s+)?(?:static\s+)?(?:async\s+)?"
    r"(?:def|class|function\*?|interface|enum|struct|fn|func|trait|impl|module)\s+"
    r"([A-Za-z_$][\w$.]*)"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
    r"(?:function\b|class\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"
    r"|^ {1,4}(?:static\s+|async\s+|get\s+|set\s+)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{\s*$")
_NOT_SYMBOLS = frozenset({"if", "for", "while", "switch", "catch", "return", "else",
                          "function", "with", "constructor"})


def _outline(text: str, first_line: int = 1, cap: int = 900) -> str:
    """'L12 Foo, L40 bar, ...' for the symbols in a file's text, so a dropped
    read can be re-read as a narrow slice instead of whole (RUNS-03: half of a
    coding agent's reads were re-reads of a dropped file)."""
    out, used = [], 0
    for n, line in enumerate(text.split("\n"), first_line):
        m = _SYMBOL.match(line)
        name = m and next((g for g in m.groups() if g), None)
        if not name or name in _NOT_SYMBOLS:
            continue
        entry = f"L{n} {name}"
        if used + len(entry) + 2 > cap:
            out.append("...")
            break
        out.append(entry)
        used += len(entry) + 2
    return ", ".join(out)


def _stub(t: dict, content: str) -> str:
    """What replaces a dropped tool result. A read says which lines it was, and
    carries an outline of them."""
    if t["name"] == "read_file" and "path" in t:
        lo, hi = t["span"]
        rng = (f"lines {lo}-{int(hi)}" if hi != float("inf") else
               "whole file" if lo == 1 else f"from line {lo}")
        outline = _outline(content, lo)
        return (f"[read_file {t['path']} ({rng}, {len(content):,} chars) was dropped "
                "to keep context small."
                + (f" Outline: {outline}." if outline else "")
                + " Re-read only the slice you need (offset and limit); a whole-file "
                "read sends the same bytes again.]")
    return (f"[{t['name']} result from an earlier step "
            f"({len(content):,} chars) was dropped to keep "
            "context small. Call the tool again if you still need it.]")


def _context_chars(messages: list[dict]) -> int:
    """Rough size of what the next model call re-sends, in chars (an image
    counts as a fixed few thousand)."""
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        else:
            n += sum(len(p.get("text") or "") + (4000 if p.get("type") == "image_url" else 0)
                     for p in c or ())
        for tc in m.get("tool_calls") or ():
            n += len((tc.get("function") or {}).get("arguments") or "")
    return n


def _evict_stale_results(messages: list[dict], tool_msgs: list[dict],
                         current_round: int, edited: dict | None = None) -> list[dict]:
    """Replace big tool results from older rounds with a stub (a read keeps an
    outline). Returns the tool_msgs entries it dropped.

    The model has acted on them, and re-sending a multi-KB dump every remaining
    round costs tokens, but dropping one costs a re-read AND breaks the
    provider's prefix cache from that point on. With `tool_result_pressure_chars`
    > 0 nothing is dropped until the turn's context passes it; then the oldest
    go first, down to 60% of it, so one eviction pass covers many rounds. Order:
    reads of a file that was edited after the read (the text is stale), then the
    rest by age, and last the reads of a file being edited that were taken after
    its last edit (the model is still working from them). With the setting at 0
    it is the old rule: every big result older than `tool_result_keep_recent`
    rounds goes."""
    horizon = current_round - settings.tool_result_keep_recent
    floor = settings.tool_result_evict_chars
    pressure = getattr(settings, "tool_result_pressure_chars", 0)
    edited = edited or {}
    cands = []
    for t in tool_msgs:
        if t["round"] > horizon or t.get("evicted"):
            continue
        if t["name"] in WRITE_PINNED:
            continue  # the model's record of what it changed — never dropped
        content = messages[t["idx"]]["content"]
        if not isinstance(content, str) or len(content) <= floor:
            continue
        last_edit = edited.get(t.get("path"), -1)
        rank = 0 if last_edit > t["round"] else 2 if last_edit >= 0 else 1
        cands.append((rank, t["round"], t["idx"], t))
    if not cands:
        return []
    size = target = 0
    if pressure > 0:
        size = _context_chars(messages)
        if size <= pressure:
            return []
        target = pressure * 6 // 10
    dropped = []
    for _rank, _rnd, _idx, t in sorted(cands, key=lambda c: c[:3]):
        if pressure > 0 and size <= target:
            break
        old = messages[t["idx"]]["content"]
        new = _stub(t, old)
        messages[t["idx"]] = {**messages[t["idx"]], "content": new}
        t["evicted"] = True
        size -= len(old) - len(new)
        dropped.append(t)
    return dropped


async def _enforce_rules(content: str, rules: str,
                         conversation_id: int | None = None) -> str:
    """No-tools verification pass. flash obeys rules ~100% without tool schemas
    attached, so this reliably fixes violations the tool-laden turn let through.
    Preserves meaning and structure; only touches rule breaks. Falls back to the
    original text on any error so a failed check never blocks the reply — or on
    a rewrite the output cap cut off, which would replace a whole answer with
    half of one. `conversation_id` attributes the call's cost to its chat."""
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
        async for ev in model.complete(prompt, temperature=0.0,   # no tools -> reliably obeys
                                       conversation_id=conversation_id):
            if ev["type"] == "message":
                revised = "" if ev.get("finish_reason") == "length" else ev["content"]
        return revised.strip() or content
    except Exception:  # noqa: BLE001 — never let the check block the answer
        return content
