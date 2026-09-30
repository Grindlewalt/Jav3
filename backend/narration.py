"""The agent's own text between its tool calls, kept after the turn.

While a turn streams, the model writes a sentence or two before each round of
tool calls ("The config lives in settings.py, let me read it"). The loop
yields that as `token` events; only the closing reply was ever stored, so the
text vanished when the turn finished. The Recorder here watches the same
events a turn's driver already sees and stores each stretch of text that sat
between calls, in order, in `turn_narration` (db._migrate_narration).

What it keeps and what it does not:
  - text that ends at a `tool` event is narration: one row, placed after the
    last stored call (`after_call_id`, 0 when it opened the turn);
  - text after the last call is the reply itself, which the message row
    already holds (rewritten by the rules pass, so the stream is not it): it
    is dropped at `final`;
  - an incognito turn stores nothing (`enabled=False`): its rows would outlive
    the wipe otherwise.

The API side (`for_message`, `merge_timeline`) is pure so tests need no server.
"""
from __future__ import annotations

# one stretch of text is a sentence or a short paragraph; a runaway one is
# clipped so a looping model cannot grow the table by megabytes
MAX_SEGMENT_CHARS = 8000


class Recorder:
    """Feed it every loop event of ONE turn (`await rec.feed(event)`), then
    `await rec.link(message_id)` once the assistant reply is stored. Never
    raises: a turn is not worth failing over its own bookkeeping."""

    def __init__(self, db, conversation_id: int, *, enabled: bool = True):
        self.db = db
        self.conversation_id = conversation_id
        self.enabled = enabled
        self._buf: list[str] = []
        self._ids: list[int] = []

    async def feed(self, event: dict) -> None:
        if not self.enabled:
            return
        kind = event.get("type")
        if kind == "token":
            self._buf.append(event.get("text") or "")
        elif kind == "tool":
            await self._flush()        # a round's first call ends its narration
        elif kind == "final":
            self._buf.clear()          # what streamed last is the reply

    async def _flush(self) -> None:
        text = "".join(self._buf).strip()
        self._buf.clear()
        if not text:
            return
        try:
            async with self.db.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM tool_calls "
                "WHERE conversation_id = ?", (self.conversation_id,)) as cur:
                after = (await cur.fetchone())["m"]
            cur = await self.db.execute(
                "INSERT INTO turn_narration (conversation_id, after_call_id, text) "
                "VALUES (?, ?, ?)",
                (self.conversation_id, after, text[:MAX_SEGMENT_CHARS]))
            self._ids.append(cur.lastrowid)
            await self.db.commit()
        except Exception:  # noqa: BLE001
            pass

    async def link(self, message_id: int | None) -> None:
        """Bind this turn's rows to the reply that closed it (the caller
        commits, or this does when it has anything to bind)."""
        if not self._ids or message_id is None:
            return
        try:
            marks = ",".join("?" * len(self._ids))
            await self.db.execute(
                f"UPDATE turn_narration SET message_id = ? WHERE id IN ({marks})",
                (message_id, *self._ids))
            await self.db.commit()
        except Exception:  # noqa: BLE001
            pass


async def load(db, conversation_id: int) -> list[dict]:
    """A conversation's narration rows, oldest first."""
    async with db.execute(
        "SELECT id, message_id, after_call_id, text, created_at AS ts "
        "FROM turn_narration WHERE conversation_id = ? ORDER BY id",
        (conversation_id,)) as cur:
        return [dict(r) for r in await cur.fetchall()]


def for_message(call_ids: list[int], rows: list[dict]) -> list[dict]:
    """One reply's narration as [{"before": n, "text": ...}]: `n` is how many of
    the reply's tool calls (`call_ids`, in order, as `activity` lists them) come
    first, so 0 opens the turn and len(call_ids) trails the last call. Rows
    with the same n keep the order they were written in."""
    out = [{"before": sum(1 for i in call_ids if i <= r["after_call_id"]),
            "text": r["text"]} for r in rows]
    out.sort(key=lambda x: x["before"])
    return out


def merge_timeline(items: list[dict], rows: list[dict]) -> list[dict]:
    """Insert narration into the Logs timeline (already sorted). Each row goes
    just before the first tool call written after the one it follows, so it
    reads in order with the calls and results around it. A row bound to its
    reply (message_id) never lands past that reply: an interrupted turn's last
    words must not slide into the next turn's calls."""
    for r in rows:
        limit = len(items)
        if r.get("message_id") is not None:
            limit = next((i for i, it in enumerate(items)
                          if it["kind"] == "message" and it["id"] == r["message_id"]),
                         limit)
        idx = next((i for i, it in enumerate(items[:limit])
                    if it["kind"] == "tool" and it["id"] > r["after_call_id"]), None)
        if idx is None:
            if r.get("message_id") is not None:
                idx = limit
            else:
                # not bound to a reply (a run that died, or one still going):
                # before the first later assistant message, else at the end
                idx = next((i for i, it in enumerate(items)
                            if it["kind"] == "message" and it["role"] == "assistant"
                            and (it["ts"] or "") >= (r["ts"] or "")), len(items))
        items.insert(idx, {"kind": "narration", "id": r["id"], "text": r["text"],
                           "ts": r["ts"]})
    return items
