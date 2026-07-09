"""Tier-2 context compaction: real conversation summarization.

Tier 1 (loop.py) evicts stale tool results WITHIN a turn. This module handles
the conversation ACROSS turns: when system prompt + history approach the
model's context window, the older portion is summarized into a structured
brief with one text-only model call, and only the recent tail rides verbatim.
The checkpoint (summary + the id of the last summarized message) is persisted
on the conversation row, so a long chat pays for each compaction once — not a
re-summarize every turn. Replaces the old silent 40-message cliff.

The trigger is an EFFECTIVE window — context minus reserved output minus a
buffer for tool specs / rule injections / estimate error — not the raw window.
"""
import aiosqlite

from .agent.model import model
from .config import settings
from .memory import estimate_tokens

# The structure is the point: a free-form summary drifts, this one forces the
# summarizer to carry forward intent, state and an exact next step. Text-only
# — a stray tool call would waste the summarizer's single turn.
SUMMARY_SYSTEM = """You are compacting an assistant conversation so it can \
continue in less space. You have NO tools — tool calls will be REJECTED and \
waste your only turn. Output only the summary, no preamble.

Summarize the conversation with exactly these sections:
1. Primary request and intent — what the operator is trying to get done.
2. Key facts, files and snippets — paths, names, numbers, decisions (verbatim
   where exact wording matters).
3. Errors and fixes — what failed and what resolved it.
4. All operator messages — every request/correction, condensed but complete.
5. Pending tasks — anything asked for and not yet delivered.
6. Current work — what was in progress most recently.
7. Next step — quote the most recent explicit instruction VERBATIM."""

# conversation_id -> consecutive summarize failures. In-memory: after
# compact_failures_max the conversation falls back to the plain recent-window
# cliff instead of hammering a doomed API path; a restart re-arms it.
_failures: dict[int, int] = {}


def effective_window() -> int:
    return (settings.model_context_window - settings.model_max_tokens
            - settings.compact_buffer_tokens)


def history_tokens(history: list[dict]) -> int:
    return sum(estimate_tokens(m.get("content") or "") for m in history)


def needs_compaction(system_prompt: str, history: list[dict],
                     prior_summary: str | None) -> bool:
    total = (estimate_tokens(system_prompt)
             + estimate_tokens(prior_summary or "")
             + history_tokens(history))
    return total > effective_window()


def split_index(history: list[dict]) -> int:
    """Index where the verbatim tail starts: walk back until roughly
    compact_recent_fraction of the tokens are kept. Always summarizes at
    least one message and always keeps at least the latest one."""
    keep = history_tokens(history) * settings.compact_recent_fraction
    acc, idx = 0, len(history) - 1
    for i in range(len(history) - 1, -1, -1):
        acc += estimate_tokens(history[i].get("content") or "")
        idx = i
        if acc >= keep:
            break
    return max(1, min(idx, len(history) - 1))


def summary_messages(summary: str) -> list[dict]:
    """The summary as it enters the model's history: a user message carrying
    the brief + a resume-directly instruction, and an assistant ack, so the
    model continues instead of spending a turn re-orienting."""
    return [
        {"role": "user", "content":
            "<conversation-summary>\n" + summary + "\n</conversation-summary>\n"
            "The earlier part of this conversation was compacted into the "
            "summary above. Resume directly from where it leaves off — do not "
            "acknowledge the summary and do not recap it."},
        {"role": "assistant", "content": "Understood — continuing."},
    ]


async def _summarize(older: list[dict], prior_summary: str | None) -> str:
    parts = []
    if prior_summary:
        parts.append(f"[summary of even earlier conversation]\n{prior_summary}")
    parts += [f"[{m['role']}]\n{m.get('content') or ''}" for m in older]
    transcript = "\n\n".join(parts)[-settings.compact_transcript_max_chars:]
    out = []
    async for ev in model.complete(
            [{"role": "system", "content": SUMMARY_SYSTEM},
             {"role": "user", "content": transcript}], temperature=0.2):
        if ev["type"] == "message":
            out.append(ev["content"] or "")
    summary = "".join(out).strip()
    if not summary:
        raise ValueError("summarizer returned nothing")
    return summary


async def compact(db: aiosqlite.Connection, conversation_id: int,
                  rows: list[dict], prior_summary: str | None) -> str | None:
    """Summarize the older portion of `rows` ([{id, role, content}], oldest
    first), persist the checkpoint, and return the new summary. Returns None
    on failure (caller falls back to the recent-window cliff); the circuit
    breaker stops retrying a conversation that keeps failing."""
    if _failures.get(conversation_id, 0) >= settings.compact_failures_max:
        return None
    split = split_index(rows)
    older = rows[:split]
    try:
        summary = await _summarize(older, prior_summary)
    except Exception:  # noqa: BLE001 — compaction must never kill the turn
        _failures[conversation_id] = _failures.get(conversation_id, 0) + 1
        return None
    _failures.pop(conversation_id, None)
    await db.execute(
        "UPDATE conversations SET compact_summary = ?, compact_upto = ? WHERE id = ?",
        (summary, older[-1]["id"], conversation_id))
    await db.commit()
    return summary


async def load_history(db: aiosqlite.Connection,
                       conversation_id: int) -> tuple[str | None, list[dict]]:
    """(summary, rows-after-checkpoint) for a conversation. Rows are oldest
    first, each {id, role, content}. The 500-row DESC window is a sanity
    backstop, not the compaction mechanism."""
    async with db.execute(
        "SELECT compact_summary, compact_upto FROM conversations WHERE id = ?",
        (conversation_id,)) as cur:
        conv = await cur.fetchone()
    summary = conv["compact_summary"] if conv else None
    upto = (conv["compact_upto"] if conv else None) or 0
    async with db.execute(
        "SELECT id, role, content FROM messages WHERE conversation_id = ? "
        "AND id > ? ORDER BY id DESC LIMIT 500", (conversation_id, upto)) as cur:
        rows = await cur.fetchall()
    return summary, [{"id": r["id"], "role": r["role"], "content": r["content"]}
                     for r in reversed(rows)]


async def assemble(db: aiosqlite.Connection, conversation_id: int,
                   system_prompt: str) -> list[dict]:
    """The model-facing history for a turn: [summary messages?] + verbatim
    tail, compacting first if the effective window demands it. This is what
    chat.py hands to run_turn in place of the old LIMIT-40 query."""
    summary, rows = await load_history(db, conversation_id)
    if len(rows) > 1 and needs_compaction(system_prompt, rows, summary):
        new_summary = await compact(db, conversation_id, rows, summary)
        if new_summary is not None:
            summary, rows = await load_history(db, conversation_id)
        else:
            # circuit open / summarize failed: degrade to the old cliff
            rows = rows[-settings.recent_message_limit:]
    history = [{"role": r["role"], "content": r["content"]} for r in rows]
    return (summary_messages(summary) if summary else []) + history
