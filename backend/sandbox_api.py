"""Sandbox review console API.

Read side (sessions / detail) reads each gated run's evidence.json and runs it
through sandbox.classify against the *current* allowlist, so approving a
destination immediately re-colours every session that touched it. Write side
(connection allow/deny, approve/quarantine, rules) is the operator's control
surface. Nothing here interprets agent-produced strings — they are only
compared, counted, and echoed back escaped by the JSON layer.
"""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import sandbox, staging
from .auth import require_user
from .config import settings
from .db import get_db

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"],
                   dependencies=[Depends(require_user)])


def _evidence_path(run_id: int) -> Path:
    return settings.vm_dir / "captures" / f"gate-{run_id}-evidence.json"


def _load_evidence(run_id: int) -> dict | None:
    p = _evidence_path(run_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


async def _runs_with_evidence() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT r.id, r.status, r.created_at, p.slug AS project "
            "FROM runs r JOIN projects p ON p.id = r.project_id "
            "ORDER BY r.id DESC LIMIT 100")
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    return rows


@router.get("/sessions")
async def sessions():
    rows = await _runs_with_evidence()
    idx = await sandbox.rules_index()
    out = []
    for r in rows:
        ev = _load_evidence(r["id"])
        if ev is None:
            continue
        c = sandbox.classify(ev, idx)
        f = c["facts"]
        out.append({
            "id": r["id"], "project": r["project"],
            "command": ev.get("command", ""),
            "created_at": ev.get("created_at") or r["created_at"],
            "verdict": c["verdict"], "rule": c["rule"], "headline": c["headline"],
            "counts": {"dns": f["dns"], "egress": f["egress_dests"],
                       "blocked": f["blocked_attempts"], "sensitive": f["sensitive"],
                       "execs": f["execs"], "staged": f["staged"]},
            "decided": r["status"] in ("approved", "quarantined"),
        })
    return {"sessions": out}


@router.get("/sessions/{run_id}")
async def session_detail(run_id: int):
    ev = _load_evidence(run_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="no evidence for this run")
    idx = await sandbox.rules_index()
    c = sandbox.classify(ev, idx)
    return {
        "id": run_id, "project": ev.get("project", ""),
        "command": ev.get("command", ""), "created_at": ev.get("created_at"),
        "exit_status": ev.get("exit_status"), "timed_out": ev.get("timed_out"),
        "egress_locked": ev.get("egress_locked"), "fresh": ev.get("fresh"),
        **c,
    }


class ConnDecision(BaseModel):
    key: str                 # "host:port" as shown in the row
    decision: str            # "allow" | "deny"


@router.post("/sessions/{run_id}/connection")
async def decide_connection(run_id: int, body: ConnDecision):
    ev = _load_evidence(run_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="no such session")
    idx = await sandbox.rules_index()
    c = sandbox.classify(ev, idx)
    row = next((e for e in c["egress"] if e["key"] == body.key), None)
    if row is None:
        raise HTTPException(status_code=404, detail="no such connection in session")
    if body.decision == "allow":
        rule = await sandbox.add_rule(
            dest=row["host"], ip=row["ip"], port=row["port"], proto=row["proto"],
            scope=row["scope"], note=f"run {run_id}")
        return {"ok": True, "rule": rule}
    if body.decision == "deny":
        # deny-by-default already blocks it; nothing to program. Recorded as a
        # no-op so the UI can confirm the choice.
        return {"ok": True, "denied": row["key"]}
    raise HTTPException(status_code=400, detail="decision must be allow or deny")


async def _set_run_status(run_id: int, status: str, pushed: int) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE runs SET status=?, pushed=? WHERE id=?",
                         (status, pushed, run_id))
        await db.commit()
    finally:
        await db.close()


@router.post("/sessions/{run_id}/approve")
async def approve_session(run_id: int):
    ev = _load_evidence(run_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="no such session")
    paths = ev.get("staged") or []
    approved = staging.approve(ev["project"], paths or None) if paths else []
    await _set_run_status(run_id, "approved", 1)
    return {"ok": True, "approved": approved}


@router.post("/sessions/{run_id}/quarantine")
async def quarantine_session(run_id: int):
    ev = _load_evidence(run_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="no such session")
    paths = ev.get("staged") or []
    if paths:
        staging.reject(ev["project"], paths)
    await _set_run_status(run_id, "quarantined", 0)
    return {"ok": True}


@router.get("/rules")
async def get_rules():
    return {"rules": await sandbox.list_rules()}


@router.delete("/rules/{rule_id}")
async def revoke_rule(rule_id: int):
    ok = await sandbox.delete_rule(rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="no such rule")
    return {"ok": True}
