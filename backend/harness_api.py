"""Harness self-report surface (report_harness_fault).

A TEMPORARY diagnostic channel while the harness is hardened for the dsh bridge:
agents log when the HARNESS itself misbehaved (a tool that errored on valid
input, a documented capability that did not work). Rows land in `harness_faults`
via backend.security.record_harness_fault, which also raises a low-severity
security event so the operator sees each one in the Review Center. This endpoint
is the plain list view — read-only, operator-authenticated.
"""
from fastapi import APIRouter, Depends

from . import security
from .auth import require_user
from .db import get_db

router = APIRouter(prefix="/api", tags=["harness"],
                   dependencies=[Depends(require_user)])


@router.get("/harness_faults")
async def harness_faults(limit: int = 100):
    limit = max(1, min(int(limit or 100), 500))
    db = await get_db()
    try:
        faults = await security.list_harness_faults(db, limit=limit)
    finally:
        await db.close()
    return {"faults": faults}
