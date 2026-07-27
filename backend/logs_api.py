"""Transcript / log viewer: scroll everything a conversation actually did.

The chat sidebar hides tool calls; this exposes the full interleaved timeline
(user + assistant messages and every tool call with its args and result) plus
the numbers that explain a token blow-up — tool-call counts, result bytes, and
the real token usage recorded per turn. Read-only.
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .agent.model import CAPTURE_STATE_KEY
from .auth import require_user
from .config import settings
from .db import get_db, get_state, set_state

router = APIRouter(prefix="/api/logs", tags=["logs"],
                   dependencies=[Depends(require_user)])


def _prices(model: str | None) -> dict:
    return settings.model_prices.get(model or "", {
        "cache_hit": settings.price_cache_hit_per_m,
        "cache_miss": settings.price_cache_miss_per_m,
        "output": settings.price_output_per_m})


def _cost_usd(cache_hit: int, cache_miss: int, output: int,
              model: str | None = None) -> float:
    p = _prices(model)
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
                    "calls": r["n"], "cost_usd": round(cost, 4)}
            agg["cost_usd"] = round(agg["cost_usd"], 4)
            out[label] = {**agg, "by_model": by_model}
        capture = await get_state(db, CAPTURE_STATE_KEY) == "1"
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
    """Opt into storing the exact message array sent per model call. Heavy
    (each ReAct iteration re-sends the grown context), so blobs age out after
    settings.context_capture_keep_days."""
    db = await get_db()
    try:
        await set_state(db, CAPTURE_STATE_KEY, "1" if body.enabled else "0")
        await db.commit()
    finally:
        await db.close()
    return {"ok": True, "enabled": body.enabled}


@router.get("/conversations/{cid}/calls")
async def model_calls(cid: int):
    """Per-API-call breakdown for one conversation: turn N = the Nth call,
    each carrying the exact token bill the provider reported."""
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, model, input_tokens, output_tokens, cache_hit, "
            "cache_miss, LENGTH(context) AS context_bytes, created_at "
            "FROM model_calls WHERE conversation_id=? ORDER BY id", (cid,))
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    for r in rows:
        r["cost_usd"] = round(
            _cost_usd(r["cache_hit"], r["cache_miss"], r["output_tokens"]), 6)
        r["has_context"] = bool(r.pop("context_bytes"))
    return {"calls": rows}


@router.get("/calls/{call_id}/context")
async def call_context(call_id: int):
    """The raw context of one captured call — exactly what went to the API."""
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT context, input_tokens, cache_hit, cache_miss "
            "FROM model_calls WHERE id=?", (call_id,))
        row = await cur.fetchone()
    finally:
        await db.close()
    if row is None:
        raise HTTPException(status_code=404, detail="no such call")
    if not row["context"]:
        raise HTTPException(status_code=404,
                            detail="no context captured for this call "
                            "(capture was off, or the blob aged out)")
    payload = json.loads(row["context"])
    return {**payload, "input_tokens": row["input_tokens"],
            "cache_hit": row["cache_hit"], "cache_miss": row["cache_miss"]}


@router.get("/conversations")
async def conversations(kind: str | None = None):
    db = await get_db()
    try:
        q = (
            "SELECT c.id, c.kind, c.summary, c.started_at, p.slug AS project, "
            "  (SELECT COUNT(*) FROM tool_calls t WHERE t.conversation_id=c.id) AS tool_calls, "
            "  (SELECT COALESCE(SUM(LENGTH(t.result)),0) FROM tool_calls t WHERE t.conversation_id=c.id) AS result_bytes, "
            # model_calls, not usage_log: the ledger covers every call (agents,
            # schedules, research, funnel nodes) — usage_log only sees chat turns
            "  (SELECT COALESCE(SUM(m.input_tokens),0) FROM model_calls m WHERE m.conversation_id=c.id) AS input_tokens, "
            "  (SELECT COALESCE(SUM(m.output_tokens),0) FROM model_calls m WHERE m.conversation_id=c.id) AS output_tokens "
            "FROM conversations c LEFT JOIN projects p ON p.id=c.project_id ")
        args: tuple = ()
        if kind:
            q += "WHERE c.kind = ? "
            args = (kind,)
        q += "ORDER BY c.id DESC LIMIT 200"
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
