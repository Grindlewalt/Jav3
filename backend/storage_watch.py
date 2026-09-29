"""Storage watch: raw-context capture is on by default, so the price of that is
one small check. At startup and then hourly it applies retention, measures what
capture is holding, the database and the free disk, and when something is over
its limit it tells the operator ONCE — a notice on the shared notices stream
(a toast in the web app, the sidebar in the terminal client), at most once a
day while it stays over. It never deletes anything past the retention the
operator chose, and it is not a security event.

The same numbers feed the Logs page (GET /api/logs/storage), so a missed
toast is still visible where the fix is."""
import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from . import ctxstore
from .config import settings
from .db import get_db, get_state, set_state

log = logging.getLogger("jav3.storage")

STATE_KEY = "storage_watch"     # JSON {"checked_at", "over": [kinds], "notified_at"}
FIX = "Lower retention or delete older captured context on Security > Logs > Cost."


def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if n >= 100 or unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def thresholds() -> dict:
    return {"captured_bytes": settings.storage_captured_warn_mb * 1024 * 1024,
            "db_bytes": settings.storage_db_warn_mb * 1024 * 1024,
            "free_pct": settings.storage_free_warn_pct}


async def stats(db) -> dict:
    """Everything the check and the Logs page need, from three cheap reads."""
    cap = await ctxstore.captured(db)
    async with db.execute("PRAGMA page_size") as cur:
        page = (await cur.fetchone())[0]
    async with db.execute("PRAGMA freelist_count") as cur:
        free_pages = (await cur.fetchone())[0]
    path = Path(settings.db_path)
    file_bytes = sum(os.path.getsize(p) for p in (path, Path(f"{path}-wal"))
                     if p.exists())
    reusable = free_pages * page
    try:
        du = shutil.disk_usage(path.parent)
        disk_total, disk_free = du.total, du.free
    except OSError:
        disk_total = disk_free = 0
    return {
        "captured_bytes": cap["bytes"], "captured_calls": cap["calls"],
        "captured_oldest": cap["oldest"],
        "db_file_bytes": file_bytes, "db_reusable_bytes": reusable,
        "db_used_bytes": max(file_bytes - reusable, 0),
        "disk_total_bytes": disk_total, "disk_free_bytes": disk_free,
        "disk_free_pct": round(100 * disk_free / disk_total, 1) if disk_total else None,
    }


def over(st: dict, limits: dict | None = None) -> list[dict]:
    """The limits currently exceeded: [{"kind", "text"}]."""
    lim = limits or thresholds()
    out = []
    if st["captured_bytes"] > lim["captured_bytes"]:
        out.append({"kind": "captured", "text":
                    f"captured model context is {fmt_bytes(st['captured_bytes'])} "
                    f"across {st['captured_calls']:,} calls "
                    f"(limit {fmt_bytes(lim['captured_bytes'])})"})
    if st["db_used_bytes"] > lim["db_bytes"]:
        extra = (f", {fmt_bytes(st['db_reusable_bytes'])} more is reusable"
                 if st["db_reusable_bytes"] > 0.1 * st["db_used_bytes"] else "")
        out.append({"kind": "db", "text":
                    f"the database holds {fmt_bytes(st['db_used_bytes'])}{extra} "
                    f"(limit {fmt_bytes(lim['db_bytes'])})"})
    pct = st["disk_free_pct"]
    if pct is not None and pct < lim["free_pct"]:
        out.append({"kind": "disk", "text":
                    f"the disk holding the database has {pct:g}% free "
                    f"({fmt_bytes(st['disk_free_bytes'])}; warning under "
                    f"{lim['free_pct']:g}%)"})
    return out


def message(reasons: list[dict]) -> str:
    body = "; ".join(r["text"] for r in reasons)
    return f"{body[0].upper()}{body[1:]}. {FIX}"


async def _load_state(db) -> dict:
    try:
        d = json.loads(await get_state(db, STATE_KEY) or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def notify(reasons: list[dict], text: str) -> None:
    """One notice on the shared notices stream. Clients that do not know the
    type ignore it, so an old tab is not broken by it."""
    from . import bus
    from .agents_run import NOTICE_CHAN
    bus.publish(NOTICE_CHAN, {
        "type": "storage_warning", "title": "Storage is filling up",
        "summary": text, "kinds": [r["kind"] for r in reasons],
        "to": "/security/logs"})


async def check(now: float | None = None) -> dict:
    """One pass: retention, measure, and a notice if over and one is due.
    Returns {"stats", "over", "notified"}."""
    now = time.time() if now is None else now
    db = await get_db()
    try:
        try:
            await ctxstore.prune(db, await ctxstore.keep_days(db))
            await db.commit()
        except Exception:  # noqa: BLE001 — measuring matters more than pruning
            log.exception("retention pass failed")
        st = await stats(db)
        reasons = over(st)
        state = await _load_state(db)
        notified = False
        if reasons and now - float(state.get("notified_at") or 0) >= settings.storage_notify_repeat_s:
            text = message(reasons)
            log.warning("storage watch: %s", text)
            try:
                notify(reasons, text)
            except Exception:  # noqa: BLE001 — a notice failing must not stop the watch
                log.exception("storage notice failed")
            state["notified_at"] = now
            notified = True
        state.update(checked_at=now, over=[r["kind"] for r in reasons])
        await set_state(db, STATE_KEY, json.dumps(state))
    finally:
        await db.close()
    return {"stats": st, "over": reasons, "notified": notified}


async def status() -> dict:
    """The current picture without side effects, for the Logs page."""
    db = await get_db()
    try:
        st = await stats(db)
        state = await _load_state(db)
        days = await ctxstore.keep_days(db)
        on = await ctxstore.capture_enabled(db)
    finally:
        await db.close()
    reasons = over(st)
    return {**st, "capture_on": on, "keep_days": days,
            "over": reasons, "message": message(reasons) if reasons else None,
            "limits": thresholds(), "last_checked": state.get("checked_at"),
            "last_notified": state.get("notified_at")}


async def watch_loop() -> None:
    """Started with the app (main.py lifespan): a check a few seconds after
    startup, then every storage_watch_interval_s."""
    if not settings.storage_watch_enabled:
        return
    await asyncio.sleep(5)
    while True:
        try:
            await check()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a watcher that dies is worse than one that skips
            log.exception("storage check failed")
        await asyncio.sleep(max(60, settings.storage_watch_interval_s))
