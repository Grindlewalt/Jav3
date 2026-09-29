"""The host gateway's refusal log (Security > Calls in the terminal client).

Every place the gateway turns a guest's request away (an op_id that is not the
caller's turn, a spent budget, an op the box's kind may not use) leaves one
short row here, next to the model_calls ledger of what it did serve. The fields
come from the host's own identity for the caller (the box, the bound turn's
project); the op name and reason are truncated to printable text because the
op name is whatever the guest typed. Best effort: a failed write must never
change what the gateway answers.
"""
from ..db import get_db

# a compromised guest can ask for a refusal as fast as it can send lines; the
# log keeps the newest rows only
KEEP = 5000


def _clean(v, n: int = 80) -> str | None:
    if v is None:
        return None
    s = "".join(ch for ch in str(v) if ch.isprintable())[:n]
    return s or None


async def record_refusal(op_name, reason: str, box_id=None,
                         project_slug=None) -> None:
    try:
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO gateway_refusals (op_name, reason, box_id, project_slug) "
                "VALUES (?, ?, ?, ?)",
                (_clean(op_name), _clean(reason) or "refused", _clean(box_id),
                 _clean(project_slug)))
            await db.execute(
                "DELETE FROM gateway_refusals WHERE id <= "
                "(SELECT MAX(id) FROM gateway_refusals) - ?", (KEEP,))
            await db.commit()
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — the log must never fail a gateway reply
        pass
