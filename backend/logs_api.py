"""Transcript / log viewer: scroll everything a conversation actually did.

The chat sidebar hides tool calls; this exposes the full interleaved timeline
(user + assistant messages and every tool call with its args and result) plus
the numbers that explain a token blow-up — tool-call counts, result bytes, and
the real token usage recorded per turn. Read-only.
"""
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from . import ctxstore, providers, storage_watch
from .config import settings
from .ctxstore import CAPTURE_STATE_KEY
from .db import get_db, set_state

router = APIRouter(prefix="/api/logs", tags=["logs"],
                   dependencies=[Depends(require_user)])


def _prices(model: str | None) -> dict | None:
    """$/Mtok for a ledgered model id, from the provider catalogue. A model
    the catalogue lists without prices is None (costed 0, flagged unpriced);
    one it doesn't list at all keeps the flat configured fallback."""
    known, p = providers.price_for(model)
    if p is not None:
        return {"cache_hit": p["cache"], "cache_miss": p["in"], "output": p["out"]}
    if known:
        return None
    return {"cache_hit": settings.price_cache_hit_per_m,
            "cache_miss": settings.price_cache_miss_per_m,
            "output": settings.price_output_per_m}


def _cost_usd(cache_hit: int, cache_miss: int, output: int,
              model: str | None = None) -> float:
    p = _prices(model)
    if p is None:
        return 0.0
    return (cache_hit * p["cache_hit"] + cache_miss * p["cache_miss"]
            + output * p["output"]) / 1_000_000


# --- cost accounting: every API call ledgered at the Model.complete choke
# point (model_calls), so headless agents / schedules / research / funnel
# nodes are all counted — usage_log only ever saw chat turns.

_WINDOWS = (("24h", "-1 day"), ("7d", "-7 days"), ("30d", "-30 days"),
            ("all", None))


@router.get("/costs")
async def costs():
    db = await get_db()
    try:
        out = {}
        for label, offset in _WINDOWS:
            # priced per model so a pro turn bills at pro rates
            q = ("SELECT model, COUNT(*) n, COALESCE(SUM(cache_hit),0) ch, "
                 "COALESCE(SUM(cache_miss),0) cm, "
                 "COALESCE(SUM(output_tokens),0) o FROM model_calls")
            args: tuple = ()
            if offset:
                q += " WHERE created_at >= datetime('now', ?)"
                args = (offset,)
            q += " GROUP BY model"
            async with db.execute(q, args) as cur:
                rows = await cur.fetchall()
            agg = {"calls": 0, "cache_hit": 0, "cache_miss": 0, "output": 0,
                   "cost_usd": 0.0}
            by_model = {}
            for r in rows:
                cost = _cost_usd(r["ch"], r["cm"], r["o"], r["model"])
                agg["calls"] += r["n"]
                agg["cache_hit"] += r["ch"]
                agg["cache_miss"] += r["cm"]
                agg["output"] += r["o"]
                agg["cost_usd"] += cost
                by_model[r["model"] or "?"] = {
                    "calls": r["n"], "cost_usd": round(cost, 4),
                    "priced": _prices(r["model"]) is not None}
            agg["cost_usd"] = round(agg["cost_usd"], 4)
            out[label] = {**agg, "by_model": by_model}
        capture = await ctxstore.capture_enabled(db)
    finally:
        await db.close()
    return {"windows": out, "capture_context": capture,
            "prices_per_m": {"cache_hit": settings.price_cache_hit_per_m,
                             "cache_miss": settings.price_cache_miss_per_m,
                             "output": settings.price_output_per_m}}


class CaptureToggle(BaseModel):
    enabled: bool


@router.post("/capture-context")
async def capture_context(body: CaptureToggle):
    """Switch storing the exact message array sent per model call on or off.
    It is ON unless switched off here (no row = on). Blobs are compressed and
    delta-coded (backend/ctxstore.py) and age out after the retention days."""
    db = await get_db()
    try:
        await set_state(db, CAPTURE_STATE_KEY, "1" if body.enabled else "0")
        await db.commit()
    finally:
        await db.close()
    return {"ok": True, "enabled": body.enabled}


@router.get("/storage")
async def storage():
    """What capture is holding, the database and the disk, against the limits
    the storage watch warns at (backend/storage_watch.py)."""
    return await storage_watch.status()


class Retention(BaseModel):
    days: int


@router.post("/capture-retention")
async def capture_retention(body: Retention):
    """Choose how many days of captured context to keep (1/3/7/14/30). A
    shorter choice is applied at once, not at the next hourly pass."""
    if body.days not in ctxstore.KEEP_CHOICES:
        raise HTTPException(status_code=400, detail="days must be one of "
                            + ", ".join(map(str, ctxstore.KEEP_CHOICES)))
    db = await get_db()
    try:
        await set_state(db, ctxstore.KEEP_STATE_KEY, str(body.days))
        before = (await ctxstore.captured(db))["bytes"]
        pruned = await ctxstore.prune(db, body.days)
        await db.commit()
        after = (await ctxstore.captured(db))["bytes"]
    finally:
        await db.close()
    return {"ok": True, "days": body.days, "deleted": pruned,
            "freed_bytes": max(before - after, 0)}


class PruneContext(BaseModel):
    older_than_days: int


@router.post("/prune-context")
async def prune_context(body: PruneContext):
    """Delete captured context older than N days, now. Token counts and costs
    stay; only the stored message arrays go. A call still inside its
    conversation's live delta chain is kept until the whole chain is old."""
    if not 1 <= body.older_than_days <= 3650:
        raise HTTPException(status_code=400, detail="older_than_days out of range")
    db = await get_db()
    try:
        before = (await ctxstore.captured(db))["bytes"]
        pruned = await ctxstore.prune(db, body.older_than_days)
        await db.commit()
        after = (await ctxstore.captured(db))["bytes"]
    finally:
        await db.close()
    return {"ok": True, "deleted": pruned, "freed_bytes": max(before - after, 0),
            "captured_bytes": after}


@router.get("/conversations/{cid}/calls")
async def model_calls(cid: int):
    """Per-API-call breakdown for one conversation: turn N = the Nth call,
    each carrying the exact token bill the provider reported."""
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, model, input_tokens, output_tokens, cache_hit, "
            "cache_miss, (context IS NOT NULL) AS has_context, created_at "
            "FROM model_calls WHERE conversation_id=? ORDER BY id", (cid,))
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    for r in rows:
        r["cost_usd"] = round(
            _cost_usd(r["cache_hit"], r["cache_miss"], r["output_tokens"],
                      r["model"]), 6)
        r["has_context"] = bool(r["has_context"])
    return {"calls": rows}


@router.get("/calls/{call_id}/context")
async def call_context(call_id: int):
    """The raw context of one captured call — exactly what went to the API."""
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT input_tokens, cache_hit, cache_miss "
            "FROM model_calls WHERE id=?", (call_id,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no such call")
        try:
            payload = await ctxstore.load(db, call_id)
        except ctxstore.ContextGone:
            raise HTTPException(status_code=404, detail="the stored context of "
                                "this call is partly gone (its retention "
                                "window passed)") from None
    finally:
        await db.close()
    if payload is None:
        raise HTTPException(status_code=404,
                            detail="no context captured for this call "
                            "(capture was off, or the blob aged out)")
    return {**payload, "input_tokens": row["input_tokens"],
            "cache_hit": row["cache_hit"], "cache_miss": row["cache_miss"]}


# octet_length() (SQLite 3.43+) reads a TEXT column's byte count from the
# record header; LENGTH() counts characters, so it pulls every overflow page of
# every tool result off disk — on the Pi that alone made this list take seconds.
_BYTELEN = "octet_length" if sqlite3.sqlite_version_info >= (3, 43) else "LENGTH"

LIST_LIMIT = 200


@router.get("/conversations")
async def conversations(kind: str | None = None):
    """The newest LIST_LIMIT conversations with their tool/token totals.

    The page is chosen first (a PK walk), then each ledger is aggregated once,
    grouped, over only those ids via its conversation_id index — not four
    correlated subqueries per row, which scanned tool_calls end to end for
    every conversation in the page."""
    where = "WHERE kind = ? " if kind else ""
    args: tuple = (kind, LIST_LIMIT) if kind else (LIST_LIMIT,)
    q = (
        f"WITH page AS (SELECT id FROM conversations {where}"
        "  ORDER BY id DESC LIMIT ?) "
        "SELECT c.id, c.kind, c.summary, c.started_at, p.slug AS project, "
        "  COALESCE(t.n, 0) AS tool_calls, COALESCE(t.b, 0) AS result_bytes, "
        "  COALESCE(m.i, 0) AS input_tokens, COALESCE(m.o, 0) AS output_tokens "
        "FROM page JOIN conversations c ON c.id = page.id "
        "LEFT JOIN projects p ON p.id = c.project_id "
        "LEFT JOIN (SELECT conversation_id AS cid, COUNT(*) AS n, "
        f"    SUM({_BYTELEN}(result)) AS b FROM tool_calls "
        "  WHERE conversation_id IN (SELECT id FROM page) "
        "  GROUP BY conversation_id) t ON t.cid = c.id "
        # model_calls, not usage_log: the ledger covers every call (agents,
        # schedules, research, funnel nodes) — usage_log only sees chat turns
        "LEFT JOIN (SELECT conversation_id AS cid, SUM(input_tokens) AS i, "
        "    SUM(output_tokens) AS o FROM model_calls "
        "  WHERE conversation_id IN (SELECT id FROM page) "
        "  GROUP BY conversation_id) m ON m.cid = c.id "
        "ORDER BY c.id DESC")
    db = await get_db()
    try:
        cur = await db.execute(q, args)
        return {"conversations": [dict(r) for r in await cur.fetchall()]}
    finally:
        await db.close()


@router.get("/conversations/{cid}")
async def transcript(cid: int):
    db = await get_db()
    try:
        cur = await db.execute("SELECT id, kind, summary FROM conversations WHERE id=?", (cid,))
        conv = await cur.fetchone()
        if not conv:
            raise HTTPException(status_code=404, detail="no such conversation")
        cur = await db.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE conversation_id=? ORDER BY id", (cid,))
        items = [{"kind": "message", "id": r["id"], "role": r["role"],
                  "content": r["content"] or "", "ts": r["created_at"]}
                 for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT id, tool, args, result, created_at FROM tool_calls "
            "WHERE conversation_id=? ORDER BY id", (cid,))
        hist: dict[str, dict] = {}
        n_calls = tot_bytes = 0
        for r in await cur.fetchall():
            res = r["result"] or ""
            items.append({"kind": "tool", "id": r["id"], "tool": r["tool"],
                          "args": r["args"] or "", "result": res,
                          "result_bytes": len(res), "ts": r["created_at"]})
            h = hist.setdefault(r["tool"], {"tool": r["tool"], "count": 0, "bytes": 0})
            h["count"] += 1
            h["bytes"] += len(res)
            n_calls += 1
            tot_bytes += len(res)
        # model_calls covers every execution path; usage_log only chat turns
        cur = await db.execute(
            "SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
            "COALESCE(SUM(cache_hit),0) ch, COALESCE(SUM(cache_miss),0) cm, COUNT(*) turns "
            "FROM model_calls WHERE conversation_id=?", (cid,))
        u = await cur.fetchone()
        # interleave by wall-clock: message and tool ids come from separate
        # sequences, so only the timestamp orders them across streams. Tool
        # calls share the second of the turn that made them, so on a tie order
        # user message -> tools -> assistant message (the real sequence).
        def _rank(x):
            if x["kind"] == "tool":
                return 1
            return 0 if x["role"] == "user" else 2
        items.sort(key=lambda x: (x["ts"] or "", _rank(x), x["id"]))
        return {
            "id": cid, "kind": conv["kind"], "summary": conv["summary"],
            "timeline": items,
            "tool_histogram": sorted(hist.values(), key=lambda h: -h["bytes"]),
            "stats": {
                "tool_calls": n_calls, "result_bytes": tot_bytes,
                "input_tokens": u["i"], "output_tokens": u["o"],
                "cache_hit": u["ch"], "cache_miss": u["cm"], "turns": u["turns"],
            },
        }
    finally:
        await db.close()


# --- Security > Calls: how the model key is used ------------------------------
# Every model call the host made (the model_calls ledger: which op, which box,
# the token bill) and every request the gateway turned away, on one timeline.

CALLS_LIMIT = 300


def _key_hosts(models) -> list[str]:
    """Host names the provider key can be sent to: the default model's provider,
    plus the provider of every model named explicitly in `models`. Only
    providers that hold a real key count. Never the key, or a URL path."""
    from urllib.parse import urlparse
    hosts: set[str] = set()
    names = [None] + [m for m in models if m and providers.split_id(m)[0]]
    for name in names:
        try:
            route = providers.resolve(name)
        except providers.ProviderError:
            continue
        if route.key and route.key != "local" and not route.key_error:
            host = urlparse(route.base_url).hostname
            if host:
                hosts.add(host)
    return sorted(hosts)


@router.get("/calls")
async def calls_log(hours: int = 24, conversation_id: int | None = None,
                    limit: int = CALLS_LIMIT):
    """The window's model calls and gateway refusals merged by time, newest
    first (capped), with totals over the whole window. `conversation_id` keeps
    that conversation's calls only: a refusal names no conversation, so none
    are listed then."""
    hours = max(1, min(hours, 24 * 365))
    limit = max(1, min(limit, 500))
    since = f"-{hours} hours"
    where, args = "m.created_at >= datetime('now', ?)", [since]
    if conversation_id is not None:
        where += " AND m.conversation_id = ?"
        args.append(conversation_id)
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT m.model, COUNT(*) n, COALESCE(SUM(m.cache_hit),0) ch, "
            "COALESCE(SUM(m.cache_miss),0) cm, COALESCE(SUM(m.output_tokens),0) o "
            f"FROM model_calls m WHERE {where} GROUP BY m.model", args)
        by_model = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT m.id, m.created_at AS ts, m.model, m.op_id, m.box_id, "
            "m.conversation_id, m.input_tokens, m.output_tokens, m.cache_hit, "
            "m.cache_miss, (m.context IS NOT NULL) AS has_context, "
            "p.slug AS project_slug FROM model_calls m "
            "LEFT JOIN conversations c ON c.id = m.conversation_id "
            "LEFT JOIN projects p ON p.id = c.project_id "
            f"WHERE {where} ORDER BY m.id DESC LIMIT ?", (*args, limit))
        calls = [dict(r) for r in await cur.fetchall()]
        refusals, n_refused = [], 0
        if conversation_id is None:
            cur = await db.execute(
                "SELECT COUNT(*) FROM gateway_refusals WHERE ts >= datetime('now', ?)",
                (since,))
            n_refused = (await cur.fetchone())[0]
            cur = await db.execute(
                "SELECT id, ts, op_name, reason, box_id, project_slug "
                "FROM gateway_refusals WHERE ts >= datetime('now', ?) "
                "ORDER BY id DESC LIMIT ?", (since, limit))
            refusals = [dict(r) for r in await cur.fetchall()]
        capture = await ctxstore.capture_enabled(db)
    finally:
        await db.close()
    for r in calls:
        r["kind"] = "call"
        r["has_context"] = bool(r["has_context"])
        r["cost_usd"] = round(_cost_usd(r["cache_hit"], r["cache_miss"],
                                        r["output_tokens"], r["model"]), 6)
    for r in refusals:
        r["kind"] = "refused"
    rows = sorted(calls + refusals, key=lambda r: (r["ts"] or "", r["kind"], r["id"]),
                  reverse=True)[:limit]
    n_calls = sum(m["n"] for m in by_model)
    cost = sum(_cost_usd(m["ch"], m["cm"], m["o"], m["model"]) for m in by_model)
    return {"hours": hours, "conversation_id": conversation_id,
            "key_hosts": _key_hosts([m["model"] for m in by_model]),
            "totals": {"calls": n_calls, "cost_usd": round(cost, 4),
                       "refused": n_refused},
            "rows": rows, "truncated": n_calls + n_refused > len(rows),
            "capture_context": capture}
