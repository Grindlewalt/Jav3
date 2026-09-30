"""Per-turn loop counters (RUNS-08): what the ReAct loop did that no other table
shows. The loop counts DSML recoveries, markup retries, forced conclusions,
round-cap hits, evicted tool results and re-reads of evicted ones, and ends
each turn with one `turn_stats` event (backend/agent/loop.py new_stats).
guest_turn records it here and does not pass it on; /api/logs/calls sums it.

One row per turn, next to model_calls (same conversation_id / op_id). An
incognito turn records nothing: it leaves no trace anywhere."""
from .db import get_db

COUNTERS = ("rounds", "dsml_recovered", "markup_retries", "forced_conclusion",
            "cap_hit", "evictions", "rereads")
# stop reasons the loop reports: final (an answer), cap (round limit), dead_end
# (the failure breaker withdrew the tools), budget (token budget spent)
STOPS = ("final", "cap", "dead_end", "budget")


async def record(conversation_id, op_id, ev: dict, box_id=None) -> None:
    """Insert one turn's row from a turn_stats event. Best-effort: counters
    must never break a turn."""
    def num(key):
        try:
            return max(0, int(ev.get(key) or 0))
        except (TypeError, ValueError):
            return 0
    stop = ev.get("stop") if ev.get("stop") in STOPS else "final"
    try:
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO turn_stats (conversation_id, op_id, box_id, rounds, "
                "dsml_recovered, markup_retries, forced_conclusion, cap_hit, evictions, "
                "rereads, stop) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (conversation_id or None, op_id, box_id,
                 *(num(k) for k in COUNTERS), stop))
            await db.commit()
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — a lost counter row is not a failed turn
        pass


async def summary(db, since: str, conversation_id: int | None = None) -> dict:
    """Totals over the window (`since` is an SQLite modifier, e.g. '-24 hours'),
    plus turns per stop reason."""
    where, args = "created_at >= datetime('now', ?)", [since]
    if conversation_id is not None:
        where += " AND conversation_id = ?"
        args.append(conversation_id)
    sums = ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in COUNTERS)
    cur = await db.execute(f"SELECT COUNT(*) AS turns, {sums} FROM turn_stats WHERE {where}", args)
    out = dict(await cur.fetchone())
    cur = await db.execute(
        f"SELECT stop, COUNT(*) AS n FROM turn_stats WHERE {where} GROUP BY stop", args)
    out["by_stop"] = {r["stop"]: r["n"] for r in await cur.fetchall()}
    return out
