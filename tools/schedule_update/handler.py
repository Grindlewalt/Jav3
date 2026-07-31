"""Schedules, self-serve — with the approval gate as the security boundary.

Creation is a PROPOSAL: the row lands paused with pending_approval=1, the
bell surfaces it, and only the operator's resume/pause click settles it. The
tool has no enable action on purpose: a schedule is standing autonomous
execution (headless, peak pricing auto-confirmed), so a compromised turn must
never be able to grant itself recurring runs. disable/delete only shrink
autonomy — delete is further limited to still-pending proposals, so operator
automations can't be removed from inside a turn."""
import re

from backend import runtime
from backend.agent.tools.toolctx import active_slug
from backend.config import settings
from backend.db import get_db
from backend.schedules import MIN_INTERVAL, _now, compute_next

_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _describe(row: dict) -> str:
    who = f"agent:{row['agent_slug']}" if row["kind"] == "agent" else "jarvis"
    when = (f"daily@{row['daily_at']}" if row["cadence_kind"] == "daily"
            else f"every {row['interval_minutes']}m")
    state = ("awaiting approval" if row["pending_approval"]
             else ("enabled" if row["enabled"] else "paused"))
    proj = f" in {row['project_slug']}" if row["project_slug"] else ""
    return (f"#{row['id']} '{row['name']}' {who} {when}{proj} — {state}, "
            f"next {row['next_run']}")


async def _list() -> str:
    db = await get_db()
    try:
        # a deleted schedule sits in the recently-deleted bin rather than being
        # dropped, so it has to be filtered out here or the model sees and
        # reasons about schedules the operator has already thrown away
        async with db.execute("SELECT * FROM schedules WHERE deleted_at IS NULL "
                              "ORDER BY enabled DESC, next_run") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    if not rows:
        return "no schedules exist yet"
    return "\n".join(_describe(r) for r in rows)


async def _create(name: str | None, task: str | None, kind: str,
                  agent_slug: str | None, cadence: str, daily_at: str,
                  interval_minutes: int | None,
                  project_slug: str | None) -> str:
    if not (name or "").strip() or not (task or "").strip():
        return "error: create needs a name and a task"
    if kind not in ("jarvis", "agent"):
        return "error: kind must be 'jarvis' or 'agent'"
    if kind == "agent":
        if not agent_slug:
            return "error: kind=agent needs an agent_slug"
        if not (settings.agents_dir / agent_slug / "AGENT.md").is_file():
            return (f"error: no agent named '{agent_slug}' — check the roster, "
                    "or create_agent it first.")
    if cadence == "daily":
        if not _HHMM.match(daily_at or ""):
            return "error: daily_at must be HH:MM (24h), e.g. 07:30"
        interval_minutes = None
    elif cadence == "interval":
        if not interval_minutes or int(interval_minutes) < MIN_INTERVAL:
            return f"error: interval_minutes must be an integer >= {MIN_INTERVAL}"
        interval_minutes = int(interval_minutes)
        daily_at = None
    else:
        return "error: cadence must be 'daily' or 'interval'"
    slug = project_slug or await active_slug()
    nxt = compute_next(cadence, daily_at, interval_minutes, _now())
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO schedules (name, kind, agent_slug, project_slug, task, "
            "cadence_kind, daily_at, interval_minutes, enabled, "
            "pending_approval, next_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?)",
            (name.strip(), kind, agent_slug if kind == "agent" else None, slug,
             task.strip(), cadence, daily_at, interval_minutes,
             nxt.isoformat(timespec="minutes")))
        await db.commit()
        sid = cur.lastrowid
    finally:
        await db.close()
    when = f"daily at {daily_at}" if cadence == "daily" else f"every {interval_minutes}m"
    return (f"proposed schedule #{sid} '{name.strip()}' ({when}) — created "
            "PAUSED, awaiting operator approval (bell / Schedules tab). It "
            "will not run until approved; tell the operator it's waiting.")


async def _disable(sid: int | None) -> str:
    if sid is None:
        return "error: disable needs an id (use action=list to find it)"
    db = await get_db()
    try:
        cur = await db.execute(
            "UPDATE schedules SET enabled = 0 WHERE id = ?", (sid,))
        await db.commit()
        if cur.rowcount == 0:
            return f"error: no schedule #{sid}"
    finally:
        await db.close()
    return f"schedule #{sid} paused (the operator can resume it in the GUI)"


async def _delete(sid: int | None) -> str:
    if sid is None:
        return "error: delete needs an id (use action=list to find it)"
    db = await get_db()
    try:
        cur = await db.execute(
            "DELETE FROM schedules WHERE id = ? AND pending_approval = 1", (sid,))
        await db.commit()
        if cur.rowcount == 0:
            return (f"error: schedule #{sid} doesn't exist or was already "
                    "decided on — only still-pending proposals can be "
                    "retracted; ask the operator to delete it in the GUI.")
    finally:
        await db.close()
    return f"retracted pending schedule #{sid}"


async def run(action: str, name: str | None = None, task: str | None = None,
              kind: str = "jarvis", agent_slug: str | None = None,
              cadence: str = "daily", daily_at: str = "09:00",
              interval_minutes: int | None = None,
              project_slug: str | None = None, id: int | None = None) -> str:
    if runtime.ephemeral.get():
        return "error: not available in incognito chat — schedules are durable."
    if action == "list":
        return await _list()
    if action == "create":
        return await _create(name, task, kind, agent_slug, cadence, daily_at,
                             interval_minutes, project_slug)
    if action == "disable":
        return await _disable(id)
    if action == "delete":
        return await _delete(id)
    return f"error: unknown action '{action}'"
