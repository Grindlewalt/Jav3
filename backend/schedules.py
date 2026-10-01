"""Heartbeats: run an agent or a Jav3 prompt on a schedule.

A schedule is 'do this task every day at 08:00' or 'every 6 hours'. A single
background loop (started in the app lifespan) wakes each minute, runs anything
due, records the result, and reschedules. Runs are headless.
"""
import asyncio
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import narration
from .agent.loop import db_tool_sink
from .vm.turn import run_agent_turn
from . import providers
from .agents_run import run_agent_headless
from .auth import require_user
from .db import get_db, open_conversation
from .memory import assemble_system_prompt

router = APIRouter(prefix="/api/schedules", tags=["schedules"],
                   dependencies=[Depends(require_user)])

POLL_SECONDS = 60
MIN_INTERVAL = 15  # floor on interval schedules, so a typo can't hammer the Pi

# Deleting a schedule moves it to a bin instead of dropping the row: the
# heartbeat stops the moment it's deleted, but a fat-fingered delete of a
# hand-tuned schedule is recoverable. The bin has an edge — past it the
# scheduler sweeps the row for real, so 'recently deleted' can't grow forever.
TRASH_DAYS = 30
TRASH_WINDOW = f"-{TRASH_DAYS} days"   # SQLite datetime() modifier

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
        # deleted rows count as seeded too — a dream the operator threw away
        # must not come back on the next restart
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
    # provider/model (bare = the default provider); omitted = the agent's own
    # pin, else the default model at run time
    model: str | None = None


def _checked_model(body: CreateSchedule) -> str | None:
    if not body.model:
        return None
    try:
        return providers.checked(body.model)
    except providers.ProviderError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.get("")
async def list_schedules():
    """Live schedules, plus the recently-deleted bin (last TRASH_DAYS days) —
    one call, same shape the projects list uses."""
    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM schedules WHERE deleted_at IS NULL "
            "ORDER BY enabled DESC, next_run") as cur:
            rows = await cur.fetchall()
        async with db.execute(
            "SELECT id, name, kind, agent_slug, project_slug, task, "
            "cadence_kind, daily_at, interval_minutes, model, deleted_at "
            "FROM schedules WHERE deleted_at IS NOT NULL "
            "AND deleted_at > datetime('now', ?) ORDER BY deleted_at DESC",
            (TRASH_WINDOW,)) as cur:
            deleted = await cur.fetchall()
    finally:
        await db.close()
    # next_run / last_run are naive server-local text: name the zone they are in
    tz = _now().astimezone()
    return {"schedules": [dict(r) for r in rows],
            "deleted": [dict(r) for r in deleted],
            "server_tz": {"name": tz.tzname(),
                          "utc_offset_min": int(tz.utcoffset().total_seconds() // 60)}}


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
    model = _checked_model(body)
    next_run = compute_next(body.cadence_kind, body.daily_at,
                            body.interval_minutes, _now())
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO schedules (name, kind, agent_slug, project_slug, task, "
            "cadence_kind, daily_at, interval_minutes, next_run, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (body.name, body.kind, body.agent_slug, body.project_slug, body.task,
             body.cadence_kind, body.daily_at, body.interval_minutes,
             next_run.isoformat(timespec="minutes"), model))
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
    model = _checked_model(body)
    nxt = compute_next(body.cadence_kind, body.daily_at, body.interval_minutes, _now())
    db = await get_db()
    try:
        cur = await db.execute(
            "UPDATE schedules SET name = ?, kind = ?, agent_slug = ?, "
            "project_slug = ?, task = ?, cadence_kind = ?, daily_at = ?, "
            "interval_minutes = ?, next_run = ?, model = ? "
            "WHERE id = ? AND deleted_at IS NULL",
            (body.name, body.kind, body.agent_slug, body.project_slug, body.task,
             body.cadence_kind, body.daily_at, body.interval_minutes,
             nxt.isoformat(timespec="minutes"), model, sid))
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
            async with db.execute(
                "SELECT * FROM schedules WHERE id = ? AND deleted_at IS NULL",
                (sid,)) as cur:
                row = await cur.fetchone()
            if row is not None:
                nxt = compute_next(row["cadence_kind"], row["daily_at"],
                                   row["interval_minutes"], _now())
                await db.execute("UPDATE schedules SET next_run = ? WHERE id = ?",
                                 (nxt.isoformat(timespec="minutes"), sid))
        # toggling is the operator's decision on a Jav3-proposed schedule
        # (resume = approve, pause = keep it parked) — either way it's no
        # longer awaiting one, so the bell stops showing it
        await db.execute(
            "UPDATE schedules SET enabled = ?, pending_approval = 0 "
            "WHERE id = ? AND deleted_at IS NULL",
            (1 if enabled else 0, sid))
        await db.commit()
    finally:
        await db.close()
    return {"ok": True}


@router.delete("/{sid}")
async def delete_schedule(sid: int):
    """Move to the recently-deleted bin. The heartbeat skips it from here on
    (_tick filters the bin out), but it stays restorable for TRASH_DAYS.
    pending_approval clears with it: deleting a Jav3-proposed schedule IS a
    decision, and the bell must never point at a row that's in the bin."""
    db = await get_db()
    try:
        cur = await db.execute(
            "UPDATE schedules SET deleted_at = datetime('now'), "
            "pending_approval = 0 WHERE id = ? AND deleted_at IS NULL", (sid,))
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="no such schedule")
    finally:
        await db.close()
    return {"ok": True}


@router.post("/{sid}/restore")
async def restore_schedule(sid: int):
    """Back out of the bin. next_run is recomputed for the same reason the
    enable path recomputes it: a schedule that sat deleted past its next_run
    would otherwise fire the instant it comes back."""
    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM schedules WHERE id = ? AND deleted_at IS NOT NULL",
            (sid,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="not in the deleted bin")
        nxt = compute_next(row["cadence_kind"], row["daily_at"],
                           row["interval_minutes"], _now())
        await db.execute(
            "UPDATE schedules SET deleted_at = NULL, next_run = ? WHERE id = ?",
            (nxt.isoformat(timespec="minutes"), sid))
        await db.commit()
    finally:
        await db.close()
    return {"ok": True, "next_run": nxt.isoformat(timespec="minutes")}


@router.delete("/{sid}/purge")
async def purge_schedule(sid: int):
    """Permanent: only allowed from the bin, like the projects purge."""
    db = await get_db()
    try:
        cur = await db.execute(
            "DELETE FROM schedules WHERE id = ? AND deleted_at IS NOT NULL", (sid,))
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=400,
                                detail="delete first — purge only empties the bin")
    finally:
        await db.close()
    return {"ok": True}


@router.post("/{sid}/run-now", status_code=202)
async def run_now(sid: int):
    """Start a run in the background and answer at once (ROBUST-18: it used to
    hold the request open for the whole run, and nothing stopped a second one).
    The row says `running…` until it finishes; next_run is not touched."""
    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM schedules WHERE id = ? AND deleted_at IS NULL",
            (sid,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    if row is None:
        raise HTTPException(status_code=404, detail="no such schedule")
    if not _reserve(sid):
        raise HTTPException(status_code=409, detail="this schedule is already running")
    try:
        await _mark_running(sid)
    except BaseException:
        _running.pop(sid, None)
        raise
    _launch(dict(row))
    return {"started": True}


async def _run_jarvis_headless(task: str, project_slug: str | None,
                               model: str | None = None) -> str:
    db = await get_db()
    try:
        title = "[scheduled] " + " ".join(task.split())[:40]
        conversation_id = await open_conversation(
            db, project=project_slug, title=title, kind="scheduled", commit=False)
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
            (conversation_id, task))
        await db.commit()
        active = project_slug if project_slug else None
        system_prompt = await assemble_system_prompt(db, active=active)
        # own fetch-ledger scope per run — a daily schedule re-reads the same
        # pages every morning by design
        from . import runtime
        wtoken = runtime.web_session.set(f"run:{conversation_id}")
        # pin the schedule's project so its tools hit it, not the GUI's
        # globally active project (host loop path; the guest envelope pins it)
        ptoken = runtime.active_project.set(active)
        cidtoken = runtime.conversation_id.set(conversation_id)
        final = ""
        rec = narration.Recorder(db, conversation_id)
        try:
            async for ev in run_agent_turn(conversation_id, system_prompt,
                                           [{"role": "user", "content": task}],
                                           active_project=active, model_name=model,
                                           on_tool_call=db_tool_sink(db, conversation_id)):
                await rec.feed(ev)
                if ev["type"] == "final":
                    final = ev["content"]
        finally:
            runtime.conversation_id.reset(cidtoken)
            runtime.active_project.reset(ptoken)
            runtime.web_session.reset(wtoken)
        cur = await db.execute(
            "INSERT INTO messages (conversation_id, role, content, model) "
            "VALUES (?, 'assistant', ?, ?)",
            (conversation_id, final, providers.turn_model_id(model)))
        await db.commit()
        await rec.link(cur.lastrowid)
        return final
    finally:
        await db.close()


async def _run_schedule(row: dict) -> str:
    """Run one schedule, return a short result string (also stored)."""
    try:
        if row["kind"] == "agent":
            out = await run_agent_headless(
                row["agent_slug"], row["task"],
                active=row["project_slug"] if row["project_slug"] else None,
                model=row.get("model"))
            return out["final"][:2000]
        return (await _run_jarvis_headless(row["task"], row["project_slug"],
                                           row.get("model")))[:2000]
    except Exception as e:  # noqa: BLE001 — a failing run must not kill the loop
        return f"error: {e}"


async def _sweep_trash() -> None:
    """Empty the bin past its window. 'Recently deleted' needs an edge, and
    the heartbeat is the only thing already waking up every minute."""
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM schedules WHERE deleted_at IS NOT NULL "
            "AND deleted_at <= datetime('now', ?)", (TRASH_WINDOW,))
        await db.commit()
    finally:
        await db.close()


# What last_result says while a run is in flight. A restart that ends the run
# leaves it behind; _mark_interrupted turns it into the truth at boot.
RUNNING = "running…"
_running: dict[int, "asyncio.Future | asyncio.Task"] = {}   # schedule id -> its run


def _reserve(sid: int) -> bool:
    """Synchronously hold a schedule's slot (no await between the check and the
    set), so the heartbeat and run-now cannot both start it."""
    if sid in _running:
        return False
    _running[sid] = asyncio.get_running_loop().create_future()
    return True


async def _mark_running(sid: int) -> None:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE schedules SET last_run = ?, last_result = ? "
            "WHERE id = ? AND deleted_at IS NULL",
            (_now().isoformat(timespec="minutes"), RUNNING, sid))
        await db.commit()
    finally:
        await db.close()


async def _claim(row: dict) -> bool:
    """Take a due schedule BEFORE running it: advance next_run and mark it
    running in one UPDATE, so a restart mid-run (a deploy during a two-hour
    agent run) does not run it again from scratch. The tick's list is a
    snapshot, so the UPDATE re-checks that the row is still enabled, not
    deleted and still due at the time we saw (an edit moves next_run); False
    means somebody changed it first and it is not ours to run."""
    now = _now()
    nxt = compute_next(row["cadence_kind"], row["daily_at"],
                       row["interval_minutes"], now)
    db = await get_db()
    try:
        cur = await db.execute(
            "UPDATE schedules SET next_run = ?, last_run = ?, last_result = ? "
            "WHERE id = ? AND enabled = 1 AND deleted_at IS NULL AND next_run = ?",
            (nxt.isoformat(timespec="minutes"), now.isoformat(timespec="minutes"),
             RUNNING, row["id"], row["next_run"]))
        await db.commit()
        return cur.rowcount == 1
    finally:
        await db.close()


async def _run_tracked(row: dict) -> None:
    result = await _run_schedule(row)
    now = _now()
    # a run that outlasted its interval would be due again the moment it ends:
    # step over the slots that went by while it ran
    nxt = compute_next(row["cadence_kind"], row["daily_at"],
                       row["interval_minutes"], now)
    db = await get_db()
    try:
        await db.execute(
            "UPDATE schedules SET last_result = ?, "
            "next_run = CASE WHEN next_run <= ? THEN ? ELSE next_run END WHERE id = ?",
            (result, now.isoformat(timespec="minutes"),
             nxt.isoformat(timespec="minutes"), row["id"]))
        await db.commit()
    finally:
        await db.close()


def _launch(row: dict) -> None:
    """Run a reserved schedule as a tracked task of its own."""
    sid = row["id"]
    task = asyncio.create_task(_run_tracked(row))
    _running[sid] = task

    def _done(t: asyncio.Task) -> None:
        if _running.get(sid) is t:
            _running.pop(sid, None)
        if not t.cancelled():
            t.exception()                    # retrieved: never an "unretrieved" warning
    task.add_done_callback(_done)


async def _mark_interrupted() -> None:
    """At boot: a row still saying `running…` lost its run to the restart."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE schedules SET last_result = ? WHERE last_result = ?",
            ("interrupted by a restart; it runs again at its next time", RUNNING))
        await db.commit()
    finally:
        await db.close()


async def _tick() -> None:
    now = _now()
    await _sweep_trash()
    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND deleted_at IS NULL "
            "AND next_run <= ?",
            (now.isoformat(timespec="minutes"),)) as cur:
            due = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    # claim each, then run it as a task of its own: one slow schedule no longer
    # holds the others behind it, and a schedule still running (or started by
    # run-now) is skipped rather than started twice
    for row in due:
        if not _reserve(row["id"]):
            continue
        try:
            claimed = await _claim(row)
        except BaseException:
            _running.pop(row["id"], None)
            raise
        if claimed:
            _launch(row)
        else:
            _running.pop(row["id"], None)


async def scheduler_loop() -> None:
    """Background heartbeat. Never lets one bad tick stop the clock."""
    try:
        await _mark_interrupted()
    except Exception:  # noqa: BLE001
        pass
    while True:
        try:
            await _tick()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(POLL_SECONDS)
