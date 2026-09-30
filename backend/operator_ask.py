"""Blocking questions to the operator: the ask_user tool and the permission
gate's "may it run?" (backend/permissions.py) both park a turn here.

    tool handler -> ask(questions) -> bus event {"type": "ask_user", id,
        conversation_id, questions[, kind, tool, args]} on the turn's channel
        (and on every ancestor chat's, so an orchestrator sees its agent ask)
    -> the operator answers in the TUI or the web chat
    -> POST /api/chat/<cid>/answer {id, answers | skipped}
    -> answer() -> ask() returns {"answers": [...]} / {"skipped": True}

One future per ask, keyed by a server-made id (the model never names it),
with a long timeout so a walked-away operator cannot hold a turn forever, and
a cancel for the operator's stop. Same shape as localexec.py's waiting, but
any authenticated operator actor may answer (the web and the TUI are both the
operator's), from the asking conversation or any ancestor that showed it.

A question is {question, options (2-5 labels), multi_select}. The UIs always
add a last free-text option ("Type something…", or `free_text_label`); an
answer is {selected: [labels], text: str | None} per question. Everything the
operator types is capped; nothing here is model-trusted beyond "the operator
said so".
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid

from . import bus, runtime

ASK_TIMEOUT_S = 60 * 60          # an unanswered ask gives up after an hour
# An orchestrator whose plan run is live waits this long, then goes back to
# supervising with the question left open (PLANS-04: an hour parked in ask_user
# meant an hour with items blocked, stalled or finished and nobody watching).
ORCH_ASK_WAIT_S = 120
MAX_QUESTIONS = 6
MAX_OPTIONS = 5
MIN_OPTIONS = 2
Q_CAP = 500                      # chars of a question
OPT_CAP = 120                    # chars of an option label
TEXT_CAP = 4000                  # chars the operator may type per question
FREE_TEXT_LABEL = "Type something…"


class AskCancelled(Exception):
    """The operator stopped the turn while an ask was waiting."""


@dataclasses.dataclass
class _Ask:
    id: str
    conversation_id: int
    shown_in: frozenset[int]         # the asking conversation + its ancestors
    event: dict
    fut: asyncio.Future
    created: float
    detached: bool = False           # nobody waits on fut: the answer is sent as a message
    chan: str = ""                   # the turn's channel (to close every view later)


_pending: dict[str, _Ask] = {}


def reset_for_tests() -> None:
    _pending.clear()


def _line(v, cap: int) -> str:
    return " ".join(str(v).split())[:cap] if isinstance(v, (str, int, float)) else ""


def clean_questions(raw) -> list[dict] | str:
    """Validate the model's questions. -> the cleaned list, or an error text."""
    if not isinstance(raw, list) or not raw:
        return "questions must be a non-empty list"
    if len(raw) > MAX_QUESTIONS:
        return f"at most {MAX_QUESTIONS} questions per ask"
    out = []
    for i, q in enumerate(raw, 1):
        if not isinstance(q, dict):
            return f"question {i} must be an object"
        text = _line(q.get("question") or q.get("text"), Q_CAP)
        if not text:
            return f"question {i} has no text"
        opts, seen = [], set()
        for o in q.get("options") or []:
            label = _line(o.get("label") if isinstance(o, dict) else o, OPT_CAP)
            if label and label.lower() not in seen:
                seen.add(label.lower())
                opts.append(label)
        if not MIN_OPTIONS <= len(opts) <= MAX_OPTIONS:
            return (f"question {i} needs {MIN_OPTIONS}-{MAX_OPTIONS} distinct options "
                    "(the operator can always type their own answer as well)")
        out.append({"question": text, "options": opts,
                    "multi_select": bool(q.get("multi_select"))})
    return out


def clean_answers(questions: list[dict], raw) -> list[dict] | None:
    """The operator's answers, checked against the questions. None = malformed."""
    if not isinstance(raw, list) or len(raw) != len(questions):
        return None
    out = []
    for q, a in zip(questions, raw):
        if not isinstance(a, dict):
            return None
        sel = a.get("selected") or []
        if isinstance(sel, str):
            sel = [sel]
        if not isinstance(sel, list) or any(s not in q["options"] for s in sel):
            return None
        sel = list(dict.fromkeys(sel))
        text = a.get("text")
        text = text.strip()[:TEXT_CAP] if isinstance(text, str) else ""
        if not q["multi_select"] and len(sel) + (1 if text else 0) > 1:
            return None
        if not sel and not text:
            return None
        out.append({"selected": sel, "text": text or None})
    return out


async def _ancestors(cid: int) -> list[int]:
    """cid's parents up to the root (nearest first); [] on any trouble."""
    from .db import get_db
    try:
        db = await get_db()
    except Exception:  # noqa: BLE001
        return []
    try:
        async with db.execute(
                "WITH RECURSIVE up(id, parent, d) AS ("
                " SELECT id, parent_conversation_id, 0 FROM conversations WHERE id = ?"
                " UNION ALL SELECT c.id, c.parent_conversation_id, up.d + 1"
                " FROM conversations c JOIN up ON c.id = up.parent WHERE up.d < 32)"
                " SELECT id FROM up WHERE d > 0 ORDER BY d", (cid,)) as cur:
            return [int(r[0]) for r in await cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []
    finally:
        await db.close()


async def _label(cid: int) -> str:
    """What to call the asking conversation in a dialog with several agents
    behind it: its generated title, else its task, else its preset; "" if none."""
    from .agenttree import clean_title
    from .db import get_db
    try:
        db = await get_db()
    except Exception:  # noqa: BLE001
        return ""
    try:
        async with db.execute("SELECT title, summary, agent_slug FROM conversations "
                              "WHERE id = ?", (cid,)) as cur:
            row = await cur.fetchone()
    except Exception:  # noqa: BLE001
        return ""
    finally:
        await db.close()
    if row is None:
        return ""
    return (clean_title(row["title"]) or clean_title(row["summary"])
            or (f"@{row['agent_slug']}" if row["agent_slug"] else ""))


def _chans(turn_chan: str, cids: list[int]) -> list[str]:
    """Where an ask shows: the turn's own channel, and each conversation's chat
    channel and agent-node channel (vm/turn.py node:<cid>), so a chat view,
    an agent view and an orchestrator's view of the tree all see it."""
    out = [turn_chan]
    for c in cids:
        out += [f"chat:{c}", f"node:{c}"]
    return list(dict.fromkeys(out))


async def _supervising_a_run(cid: int) -> bool:
    """Is `cid` an orchestrator conversation whose plan run is live right now?"""
    from .db import get_db
    try:
        db = await get_db()
    except Exception:  # noqa: BLE001
        return False
    try:
        async with db.execute(
                "SELECT c.mode AS mode, p.slug AS slug FROM conversations c "
                "LEFT JOIN projects p ON p.id = c.project_id WHERE c.id = ?",
                (cid,)) as cur:
            row = await cur.fetchone()
    except Exception:  # noqa: BLE001
        return False
    finally:
        await db.close()
    if row is None or row["mode"] != "orchestrate" or not row["slug"]:
        return False
    from . import plan
    return plan.is_running(row["slug"])


def _settled(a: _Ask) -> None:
    """Tell every view that showed an ask that it is over (answered here or
    elsewhere, timed out, cancelled, or detached and then answered), so a second
    copy closes."""
    _pending.pop(a.id, None)
    done = {"type": "ask_done", "id": a.id, "conversation_id": a.conversation_id}
    for c in _chans(a.chan, sorted(a.shown_in)):
        bus.publish(c, done)


async def ask(questions: list[dict], *, extra: dict | None = None,
              timeout: float | None = None) -> dict:
    """Publish one ask on the running turn and wait for the operator.
    -> {"answers": [...]} | {"skipped": True} | {"error": text} | {"detached": id}.
    Raises AskCancelled when the operator stops the turn meanwhile.

    {"detached": id} is for an orchestrator supervising a live plan run: after
    ORCH_ASK_WAIT_S the ask stays open for the operator but the turn is handed
    back, and the operator's answer later reaches it as a message (answer())."""
    cid = runtime.conversation_id.get()
    chan = runtime.event_chan.get()
    if cid is None or not chan:
        return {"error": "there is no operator attached to this turn to ask"}
    ancestors = await _ancestors(cid)
    ask_id = f"ask_{uuid.uuid4().hex[:16]}"
    event = {"type": "ask_user", "id": ask_id, "conversation_id": cid,
             "agent": await _label(cid), "questions": questions, **(extra or {})}
    fut = asyncio.get_running_loop().create_future()
    a = _pending[ask_id] = _Ask(ask_id, cid, frozenset([cid, *ancestors]), event, fut,
                                time.time(), chan=chan)
    wait = timeout or ASK_TIMEOUT_S
    # a permission ask (extra.kind) gates one tool call and needs its answer
    detachable = not (extra or {}).get("kind") and await _supervising_a_run(cid)
    if detachable:
        wait = min(wait, ORCH_ASK_WAIT_S)
    try:
        for c in _chans(chan, [cid, *ancestors]):
            bus.publish(c, event)
        await asyncio.wait({fut}, timeout=wait)     # (wait_for would cancel fut)
        if fut.done():
            return fut.result()
        if detachable and await _supervising_a_run(cid):
            a.detached = True
            return {"detached": ask_id}
        return {"error": f"the operator did not answer within {int(wait // 60)} minutes"}
    finally:
        if not a.detached:
            _settled(a)


def answer(cid: int, ask_id: str, answers, skipped: bool) -> str:
    """-> "ok" | "missing" | "invalid". `cid` is the conversation the answer
    was posted to: the asking one or an ancestor that displayed the ask."""
    a = _pending.get(ask_id)
    if a is None or a.fut.done() or cid not in a.shown_in:
        return "missing"
    if skipped:
        got = {"skipped": True}
    else:
        clean = clean_answers(a.event["questions"], answers)
        if clean is None:
            return "invalid"
        got = {"answers": clean}
    a.fut.set_result(got)
    if a.detached:
        _deliver_detached(a, got)
    return "ok"


def _deliver_detached(a: _Ask, got: dict) -> None:
    """The turn that asked went back to work (ask() returned {"detached"}): the
    operator's answer reaches it as a message, read after its current tool call
    like any other operator message. If its turn has ended the ask was dropped
    with it, so this only ever runs for a live one."""
    _settled(a)
    text = ("Your earlier question to the operator (asked while the plan run was "
            "live) has now been answered.\n"
            + render_result(a.event["questions"], got))

    async def _send() -> None:
        from . import agentmsg
        await agentmsg.queue_operator_message(a.conversation_id, text)
    asyncio.get_running_loop().create_task(_send())


def _cancel(a: _Ask) -> None:
    if a.detached:
        a.fut.cancel()          # nothing waits on it: just close it everywhere
        _settled(a)
    else:
        a.fut.set_exception(AskCancelled())


def cancel_conversation(cid: int) -> int:
    n = 0
    for a in list(_pending.values()):
        if a.conversation_id == cid and not a.fut.done():
            _cancel(a)
            n += 1
    return n


def cancel_tree(cids) -> int:
    """Cancel every pending ask of these conversations and of the agents beneath
    them. A stop reaches the turn or run it names, but an agent that is a node in
    a box has neither of its own: without this its ask waits out the hour and the
    agents view keeps saying it waits on the operator."""
    ids = set(cids)
    n = 0
    for a in list(_pending.values()):
        if not a.fut.done() and (a.conversation_id in ids or ids & a.shown_in):
            _cancel(a)
            n += 1
    return n


def cancel_all() -> int:
    n = 0
    for a in list(_pending.values()):
        if not a.fut.done():
            _cancel(a)
            n += 1
    return n


def pending_events(cid: int) -> list[dict]:
    """Unanswered asks a re-attaching view of `cid` should show."""
    return [a.event for a in list(_pending.values())
            if cid in a.shown_in and not a.fut.done()]


def pending_list() -> list[dict]:
    """Every unanswered ask, for notifications and the agents tree."""
    return [{"id": a.id, "conversation_id": a.conversation_id,
             "kind": a.event.get("kind", "question"),
             "question": a.event["questions"][0]["question"][:200],
             "age_s": int(time.time() - a.created)}
            for a in sorted(_pending.values(), key=lambda x: x.created)
            if not a.fut.done()]


def render_result(questions: list[dict], got: dict) -> str:
    """ask_user's tool result, as the model reads it."""
    if got.get("error"):
        return f"error: {got['error']}"
    if got.get("detached"):
        return ("The question is posted to the operator and stays open, but your plan "
                "run is live and nobody is watching it while you wait, so it does not "
                "block you. Go back to plan_status now; do not ask again. If the "
                "operator answers while you are still working it reaches you as a "
                "message. Until then decide on your best judgement, say which "
                "assumption you made, and put anything still open in your final report.")
    if got.get("skipped"):
        return ("The operator SKIPPED these questions without answering. Do not ask "
                "the same thing again right away: proceed on your best judgement "
                "and say which assumption you made, or stop if you cannot.")
    lines = ["The operator answered:"]
    for i, (q, a) in enumerate(zip(questions, got["answers"]), 1):
        lines.append(f"{i}. {q['question']}")
        picked = list(a["selected"])
        if picked:
            lines.append("   -> " + "; ".join(picked))
        if a.get("text"):
            lines.append("   -> (typed) " + a["text"])
    return "\n".join(lines)
