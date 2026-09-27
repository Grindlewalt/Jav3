from backend import runtime, security
from backend.db import get_db

# severity as the model may phrase it -> the enum the row stores
_SEV = {"low": "low", "medium": "medium", "high": "high",
        "med": "medium", "critical": "high", "hi": "high", "lo": "low"}


async def run(what_i_tried: str, what_went_wrong: str,
              what_i_expected: str | None = None, severity: str = "low") -> str:
    tried = (what_i_tried or "").strip()
    went_wrong = (what_went_wrong or "").strip()
    if not tried or not went_wrong:
        return ("error: report_harness_fault needs what_i_tried and "
                "what_went_wrong — say what you called and how it misbehaved.")
    sev = _SEV.get((severity or "low").strip().lower(), "low")
    # identity is host-side, exactly like send_message: the turn's conversation
    # and project come from the restored envelope, never from arguments.
    cid = runtime.conversation_id.get()
    project = runtime.active_project.get()
    db = await get_db()
    try:
        fault_id = await security.record_harness_fault(
            db, tried=tried, went_wrong=went_wrong,
            expected=(what_i_expected or "").strip() or None,
            severity=sev, conversation_id=cid, project=project)
    finally:
        await db.close()
    return (f"logged harness fault #{fault_id} ({sev}). Noted for the operator — "
            "now route around it and finish the task; do not retry the same call.")
