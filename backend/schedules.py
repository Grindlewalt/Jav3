"""Heartbeats: run an agent or a Jarvis prompt on a schedule.

A schedule is 'do this task every day at 08:00' or 'every 6 hours'. A single
background loop (started in the app lifespan) wakes each minute, runs anything
due, records the result, and reschedules. Runs are headless — peak pricing is
auto-confirmed because the operator set the schedule up deliberately and isn't
there to answer a prompt.
"""
import asyncio
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .agent.loop import db_tool_sink
from .vm.turn import run_agent_turn
from .agent.model import confirm_peak
from .agents_run import run_agent_headless
from .auth import require_user
from .config import settings
from .db import get_db, open_conversation
from .memory import assemble_system_prompt

router = APIRouter(prefix="/api/schedules", tags=["schedules"],
                   dependencies=[Depends(require_user)])

POLL_SECONDS = 60
MIN_INTERVAL = 15  # floor on interval schedules, so a typo can't hammer the Pi

# The nightly memory-consolidation ("dreaming") pass: merge duplicates, prune
# stale facts, keep every note described. Seeded DISABLED — the operator
# flips it on in the GUI when ready to spend nightly tokens on it.
DREAM_SCHEDULE_NAME = "Memory consolidation (dream)"
DREAM_TASK = """Consolidate your memory notes (nightly dream pass). Use only \
memory_read and memory_write; do not touch project files or the web.
Phase 1 — orient: list all notes, then read every note whose description \
overlaps another's or is missing.
Phase 2 — gather: note duplicates, contradictions, stale/superseded facts, \
relative dates, and notes without a description.
Phase 3 — consolidate: merge each duplicate set into ONE note (mode=replace, \
with a one-line description), then delete the leftovers (mode=delete). Prefer \
updating an existing note over creating a new one. Convert relative dates to \
absolute. Never weaken or drop an operator preference or rule.
Phase 4 — verify: list the notes again — every note has a clear description, \
no two cover the same topic. Reply with a short changelog of what you merged, \
deleted or rewrote (or "no changes needed")."""


async def ensure_default_schedules() -> None:
    """Idempotent seed, called from app startup after init_db."""
    db = await get_db()
    try:
        async with db.execute("SELECT 1 FROM schedules WHERE name = ?",
                              (DREAM_SCHEDULE_NAME,)) as cur:
            if await cur.fetchone():
                return
        nxt = compute_next("daily", "03:30", None, _now())
        await db.execute(
            "INSERT INTO schedules (name, kind, task, cadence_kind, daily_at, "
            "enabled, next_run) VALUES (?, 'jarvis', ?, 'daily', '03:30', 0, ?)",
            (DREAM_SCHEDULE_NAME, DREAM_TASK, nxt.isoformat(timespec="minutes")))
        await db.commit()
    finally:
        await db.close()


def _now() -> dt.datetime:
    return dt.datetime.now()


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def compute_next(cadence_kind: str, daily_at: str | None,
                 interval_minutes: int | None, after: dt.datetime) -> dt.datetime:
    if cadence_kind == "daily":
        h, m = _parse_hhmm(daily_at or "09:00")
        cand = after.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= after:
            cand += dt.timedelta(days=1)
        return cand
    minutes = max(MIN_INTERVAL, int(interval_minutes or MIN_INTERVAL))
    return after + dt.timedelta(minutes=minutes)


class CreateSchedule(BaseModel):
    name: str
    kind: str = "jarvis"          # 'agent' | 'jarvis'
    agent_slug: str | None = None
    project_slug: str | None = None
    task: str
    cadence_kind: str = "daily"   # 'daily' | 'interval'
    daily_at: str | None = "09:00"
    interval_minutes: int | None = None


@router.get("")
async def list_schedules():
    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM schedules ORDER BY enabled DESC, next_run") as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()
    return {"schedules": [dict(r) for r in rows]}


@router.post("")
async def create_schedule(body: CreateSchedule):
    if body.kind not in ("agent", "jarvis"):
        raise HTTPException(status_code=400, detail="kind must be 'agent' or 'jarvis'")
    if body.kind == "agent" and not body.agent_slug:
        raise HTTPException(status_code=400, detail="agent schedules need an agent_slug")
    if body.cadence_kind not in ("daily", "interval"):
        raise HTTPException(status_code=400, detail="cadence_kind must be 'daily' or 'interval'")
    if not body.task.strip():
        raise HTTPException(status_code=400, detail="task is required")
    next_run = compute_next(body.cadence_kind, body.daily_at,
                            body.interval_minutes, _now())
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO schedules (name, kind, agent_slug, project_slug, task, "
            "cadence_kind, daily_at, interval_minutes, next_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (body.name, body.kind, body.agent_slug, body.project_slug, body.task,
             body.cadence_kind, body.daily_at, body.interval_minutes,
             next_run.isoformat(timespec="minutes")))
        await db.commit()
        sid = cur.lastrowid
    finally:
        await db.close()
    return {"id": sid, "next_run": next_run.isoformat(timespec="minutes")}


@router.put("/{sid}")
async def update_schedule(sid: int, body: CreateSchedule):
    """Full edit. next_run is recomputed from the (possibly new) cadence so an
    edited schedule never fires off its stale timetable."""
    if body.kind not in ("agent", "jarvis"):
        raise HTTPException(status_code=400, detail="kind must be 'agent' or 'jarvis'")
    if body.kind == "agent" and not body.agent_slug:
        raise HTTPException(status_code=400, detail="agent schedules need an agent_slug")
    if body.cadence_kind not in ("daily", "interval"):
        raise HTTPException(status_code=400, detail="cadence_kind must be 'daily' or 'interval'")
    if not body.task.strip():
        raise HTTPException(status_code=400, detail="task is required")
    nxt = compute_next(body.cadence_kind, body.daily_at, body.interval_minutes, _now())
    db = await get_db()
    try:
        cur = await db.execute(
            "UPDATE schedules SET name = ?, kind = ?, agent_slug = ?, "
            "project_slug = ?, task = ?, cadence_kind = ?, daily_at = ?, "
            "interval_minutes = ?, next_run = ? WHERE id = ?",
            (body.name, body.kind, body.agent_slug, body.project_slug, body.task,
             body.cadence_kind, body.daily_at, body.interval_minutes,
             nxt.isoformat(timespec="minutes"), sid))
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="no such schedule")
    finally:
        await db.close()
    return {"ok": True, "next_run": nxt.isoformat(timespec="minutes")}


@router.patch("/{sid}")
async def toggle_schedule(sid: int, enabled: bool):
    db = await get_db()
    try:
        if enabled:
            # recompute next_run on enable: a schedule that sat disabled past
            # its next_run would otherwise fire the moment it's switched on
            async with db.execute("SELECT * FROM schedules WHERE id = ?",
                                  (sid,)) as cur:
                row = await cur.fetchone()
            if row is not None:
                nxt = compute_next(row["cadence_kind"], row["daily_at"],
                                   row["interval_minutes"], _now())
                await db.execute("UPDATE schedules SET next_run = ? WHERE id = ?",
                                 (nxt.isoformat(timespec="minutes"), sid))
        # toggling is the operator's decision on a Jarvis-proposed schedule
        # (resume = approve, pause = keep it parked) — either way it's no
        # longer awaiting one, so the bell stops showing it
        await db.execute(
            "UPDATE schedules SET enabled = ?, pending_approval = 0 WHERE id = ?",
            (1 if enabled else 0, sid))
        await db.commit()
    finally:
        await db.close()
    return {"ok": True}


@router.delete("/{sid}")
async def delete_schedule(sid: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM schedules WHERE id = ?", (sid,))
        await db.commit()
    finally:
        await db.close()
    return {"ok": True}


@router.post("/{sid}/run-now")
async def run_now(sid: int):
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM schedules WHERE id = ?", (sid,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    if row is None:
        raise HTTPException(status_code=404, detail="no such schedule")
    result = await _run_schedule(dict(row))
    return {"result": result}


async def _run_jarvis_headless(task: str, project_slug: str | None) -> str:
    db = await get_db()
    try:
        title = "[scheduled] " + " ".join(task.split())[:40]
        conversation_id = await open_conversation(
            db, project=project_slug, title=title, kind="scheduled", commit=False)
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
            (conversation_id, task))
        await db.commit()
        confirm_peak(conversation_id)
        active = project_slug if project_slug else None
        system_prompt = await assemble_system_prompt(db, active=active)
        # own fetch-ledger scope per run — a daily schedule re-reads the same
        # pages every morning by design
        from . import runtime
        wtoken = runtime.web_session.set(f"run:{conversation_id}")
        final = ""
        try:
            async for ev in run_agent_turn(conversation_id, system_prompt,
                                           [{"role": "user", "content": task}],
                                           active_project=active,
                                           on_tool_call=db_tool_sink(db, conversation_id)):
                if ev["type"] == "final":
                    final = ev["content"]
        finally:
            runtime.web_session.reset(wtoken)
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) "
            "VALUES (?, 'assistant', ?)", (conversation_id, final))
        await db.commit()
        return final
    finally:
        await db.close()


async def _run_schedule(row: dict) -> str:
    """Run one schedule, return a short result string (also stored)."""
    try:
        if row["kind"] == "agent":
            out = await run_agent_headless(
                row["agent_slug"], row["task"],
                active=row["project_slug"] if row["project_slug"] else None)
            return out["final"][:2000]
        return (await _run_jarvis_headless(row["task"], row["project_slug"]))[:2000]
    except Exception as e:  # noqa: BLE001 — a failing run must not kill the loop
        return f"error: {e}"


async def _tick() -> None:
    now = _now()
    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ?",
            (now.isoformat(timespec="minutes"),)) as cur:
            due = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    for row in due:
        result = await _run_schedule(row)
        nxt = compute_next(row["cadence_kind"], row["daily_at"],
                           row["interval_minutes"], _now())
        db = await get_db()
        try:
            await db.execute(
                "UPDATE schedules SET last_run = ?, last_result = ?, next_run = ? "
                "WHERE id = ?",
                (now.isoformat(timespec="minutes"), result,
                 nxt.isoformat(timespec="minutes"), row["id"]))
            await db.commit()
        finally:
            await db.close()


async def scheduler_loop() -> None:
    """Background heartbeat. Never lets one bad tick stop the clock."""
    while True:
        try:
            await _tick()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(POLL_SECONDS)
