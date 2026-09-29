"""Captured model-call context: how the exact message array sent per call is
stored, read back, aged out and measured.

Capture is ON by default (the operator's ask: every turn viewable without
turning anything on first). Each ReAct round re-sends the whole grown context,
so what makes that affordable is the storage form, in model_calls.context:

  - legacy rows: JSON text {"messages": [...], "n_tools": N}. Still read.
  - new rows: a BLOB, MAGIC + zlib(JSON). Either a FULL frame
    {"v":1,"messages":[...],"n_tools":N} or a DELTA frame
    {"v":1,"base":<row id>,"shared":k,"messages":[tail...],"n_tools":N} meaning
    "the base call's first k messages, then these". On the transcripts already
    on the Pi that was 196 KB raw per call, 46 KB zlib'd alone, 1.6 KB delta'd.

Deltas chain (each against the conversation's previous call) and a chain is
started over with a full frame at least every settings.context_delta_chain_max
calls, or whenever the prefix is not shared (compaction rewrote the context).
Every delta row carries ctx_key = its chain's first row, so a chain reads in
one query and ages out as one unit: a row is only nulled when the whole chain
is past retention (see prune). Nothing here can make a stored blob unreadable
except deleting its own chain, and then load() says "gone", never garbage.
"""
import hashlib
import json
import sqlite3
import time
import zlib
from collections import OrderedDict

from .config import settings
from .db import get_state

CAPTURE_STATE_KEY = "capture_context"     # "0" = the operator switched it off
KEEP_STATE_KEY = "context_keep_days"      # the Logs page's retention choice
KEEP_CHOICES = (1, 3, 7, 14, 30)
MAGIC = b"JCX1"

# octet_length() (SQLite 3.43+) reads a column's byte count from the record
# header; LENGTH() on TEXT counts characters and pulls every overflow page.
_BYTELEN = "octet_length" if sqlite3.sqlite_version_info >= (3, 43) else "LENGTH"


class ContextGone(Exception):
    """The blob's chain is missing a frame (pruned or deleted)."""


async def capture_enabled(db) -> bool:
    """On unless explicitly switched off: an install that never touched the
    toggle has no row, and no row means capturing."""
    return await get_state(db, CAPTURE_STATE_KEY) != "0"


async def keep_days(db) -> int:
    raw = await get_state(db, KEEP_STATE_KEY)
    try:
        d = int(raw) if raw is not None else settings.context_capture_keep_days
    except ValueError:
        d = settings.context_capture_keep_days
    return d if 1 <= d <= 365 else settings.context_capture_keep_days


# --- writing ---------------------------------------------------------------

class Frame:
    __slots__ = ("blob", "key", "depth", "digests", "parent")

    def __init__(self, blob: bytes, key: int | None, depth: int,
                 digests: list[bytes], parent: int | None = None):
        self.blob = blob          # what goes in model_calls.context
        self.key = key            # model_calls.ctx_key: None for a full frame
        self.depth = depth        # deltas since the last full frame
        self.digests = digests    # per message, for the next call's comparison
        self.parent = parent      # the base row of a delta


class _Head:
    __slots__ = ("row_id", "key", "depth", "digests", "at")

    def __init__(self, row_id, key, depth, digests):
        self.row_id, self.key, self.depth, self.digests = row_id, key, depth, digests
        self.at = time.monotonic()


# conversation id -> the last few calls' frames, to delta against. In-process
# on purpose (one process, require_single_process): after a restart the first
# call of a conversation is simply a full frame. A head older than _HEAD_TTL is
# dropped, so a delta never points at a row retention may already have nulled
# (the shortest retention is a day).
_heads: "OrderedDict[int, list[_Head]]" = OrderedDict()
_MAX_CONVERSATIONS = 64
_HEADS_PER_CONVERSATION = 3
_HEAD_TTL = 6 * 3600


def _digest(s: str) -> bytes:
    return hashlib.blake2b(s.encode(), digest_size=12).digest()


def _shared_prefix(a: list[bytes], b: list[bytes]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def build_frame(conversation_id: int | None, messages: list[dict],
                n_tools: int) -> Frame:
    """Serialise one call's message array (delta against the conversation's
    best matching recent call when that saves real bytes) and compress it."""
    parts = [json.dumps(m, default=str) for m in messages]
    digests = [_digest(p) for p in parts]
    total = sum(map(len, parts))
    best = None
    if settings.context_capture_delta and conversation_id is not None:
        now = time.monotonic()
        cand = [h for h in _heads.get(conversation_id, ())
                if now - h.at < _HEAD_TTL
                and h.depth < settings.context_delta_chain_max]
        scored = [(_shared_prefix(h.digests, digests), h) for h in cand]
        if scored:
            shared, head = max(scored, key=lambda t: t[0])
            # a delta that keeps under half the bytes is not worth the chain
            if shared and sum(map(len, parts[:shared])) * 2 >= total:
                best = (shared, head)
    if best is None:
        text = '{"v":1,"n_tools":%d,"messages":[%s]}' % (n_tools, ",".join(parts))
        return Frame(MAGIC + zlib.compress(text.encode(), 6), None, 0, digests)
    shared, head = best
    text = '{"v":1,"base":%d,"shared":%d,"n_tools":%d,"messages":[%s]}' % (
        head.row_id, shared, n_tools, ",".join(parts[shared:]))
    return Frame(MAGIC + zlib.compress(text.encode(), 6), head.key,
                 head.depth + 1, digests, head.row_id)


def remember(conversation_id: int | None, frame: Frame, row_id: int) -> None:
    """Note the row just inserted as a base for the conversation's next call."""
    if conversation_id is None or not settings.context_capture_delta:
        return
    head = _Head(row_id, frame.key if frame.key is not None else row_id,
                 frame.depth, frame.digests)
    heads = _heads.setdefault(conversation_id, [])
    _heads.move_to_end(conversation_id)
    # the head this frame extends is superseded by it
    heads[:] = [h for h in heads if h.row_id != frame.parent]
    heads.append(head)
    while len(heads) > _HEADS_PER_CONVERSATION:
        heads.remove(min(heads, key=lambda h: h.at))
    while len(_heads) > _MAX_CONVERSATIONS:
        _heads.popitem(last=False)


def forget_heads() -> None:
    _heads.clear()


# --- reading ---------------------------------------------------------------

def is_frame(v) -> bool:
    return isinstance(v, (bytes, bytearray, memoryview)) and bytes(v[:4]) == MAGIC


def _parse(v) -> dict:
    if is_frame(v):
        return json.loads(zlib.decompress(bytes(v)[4:]))
    return json.loads(v)          # legacy TEXT row


async def load(db, call_id: int) -> dict | None:
    """{"messages": [...], "n_tools": N} for one call, whatever its stored
    form. None: no such call, or nothing was captured for it. ContextGone: it
    was a delta and part of its chain is gone."""
    async with db.execute(
            "SELECT context, ctx_key FROM model_calls WHERE id=?", (call_id,)) as cur:
        row = await cur.fetchone()
    if row is None or row["context"] is None:
        return None
    top = _parse(row["context"])
    if "base" not in top:
        return {"messages": top["messages"], "n_tools": top.get("n_tools", 0)}
    key = row["ctx_key"]
    if key is None:
        raise ContextGone("delta without a chain")
    async with db.execute(
            "SELECT id, context FROM model_calls "
            "WHERE (id=? OR ctx_key=?) AND id<=? AND context IS NOT NULL",
            (key, key, call_id)) as cur:
        rows = {r["id"]: r["context"] for r in await cur.fetchall()}
    chain = [top]
    cur_frame, seen = top, {call_id}
    while "base" in cur_frame:
        bid = cur_frame["base"]
        if bid in seen or bid not in rows:
            raise ContextGone(f"call {bid} is gone")
        seen.add(bid)
        try:
            cur_frame = _parse(rows[bid])
        except (ValueError, zlib.error) as e:
            raise ContextGone(f"call {bid} is unreadable") from e
        chain.append(cur_frame)
    messages = list(chain[-1]["messages"])            # the full frame
    for f in reversed(chain[:-1]):                    # then each delta, oldest first
        messages = messages[:f["shared"]] + f["messages"]
    return {"messages": messages, "n_tools": top.get("n_tools", 0)}


# --- retention and measurement ----------------------------------------------

async def prune(db, days: int) -> int:
    """Null the captured context of calls older than `days`. A delta chain
    goes as a unit, when its NEWEST row is past the cutoff, so a row that is
    still inside retention never loses a frame it needs. Returns rows nulled.
    Usage rows are kept forever, as ever. Does not commit."""
    cut = f"-{int(days)} days"
    cur = await db.execute(
        "UPDATE model_calls SET context = NULL WHERE context IS NOT NULL "
        "AND created_at < datetime('now', ?) "
        "AND COALESCE(ctx_key, id) NOT IN ("
        "  SELECT ctx_key FROM model_calls WHERE ctx_key IS NOT NULL "
        "  AND context IS NOT NULL AND created_at >= datetime('now', ?))",
        (cut, cut))
    forget_heads()
    return cur.rowcount or 0


_last_prune: dict[str, float] = {}
PRUNE_EVERY_S = 600


async def prune_if_due(db) -> None:
    """The retention pass the ledger insert triggers, at most every ten
    minutes per database (the storage watch runs it hourly regardless)."""
    k = str(settings.db_path)
    now = time.monotonic()
    if now - _last_prune.get(k, -PRUNE_EVERY_S) < PRUNE_EVERY_S:
        return
    _last_prune[k] = now
    await prune(db, await keep_days(db))


async def captured(db) -> dict:
    """What the capture is holding: {"calls", "bytes", "oldest"}."""
    async with db.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM({_BYTELEN}(context)), 0) AS b, "
            "MIN(created_at) AS oldest FROM model_calls "
            "WHERE context IS NOT NULL") as cur:
        r = await cur.fetchone()
    return {"calls": r["n"], "bytes": r["b"], "oldest": r["oldest"]}
