"""Box history and what each box is doing now (the /vms screens).

History: one `box_events` row per thing that happened to a box (db.py
_migrate_boxlog), served at GET /api/vm/boxes/{id}/events and published on
the `vm-boxes` bus channel as {"type": "box_event", ...} so the VM manager
refreshes on it instead of polling hard.

    started       the guest booted (by a turn, a service, or the operator)
    stopped       the guest was killed; the box keeps its reservation
    restarted     stopped and booted again (operator)
    idle_stopped  a project box sat idle past vm_box_idle_stop_seconds and
                  was stopped and released by the reaper
    wiped         the shared box's idle scrub (vm_idle_scrub_seconds)
    nuked         the shared box's overlay discarded, rebooted fresh
    destroyed     stopped, released, its directory deleted
    crashed       the guest exited on its own (QEMU exit, dead container)
    error         a boot or an action failed; `reason` says why

Who and why: an action (nuke, destroy, the reaper's stop) runs inside
`action(box, event, ...)`, which records ONE event for the whole thing: a nuke
is a teardown and a boot, and the history says "nuked", not "stopped,
started". `by(actor)` names who asked (the operator's user name on API
routes). Outside an action, boots and teardowns are recorded as they happen
(`started` / `stopped`), attributed to the turn bound to the box, else "app".

Now: a bus tap remembers the tool each turn's event channel is running (the
`tool` / `tool_result` events the chat and agent loops already publish), so a
box row can say "turn #42 · shell: npm test" without anything new in the loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import time

from .. import bus
from . import boxes

EVENTS = ("started", "stopped", "restarted", "idle_stopped", "wiped", "nuked",
          "destroyed", "crashed", "error")
BUS_CHAN = boxes.BUS_CHAN
KEEP_ROWS = 5000                 # the table keeps the newest this many events
REASON_CHARS = 300

# the action in progress in this task: {"boxes": {ids}, "actor", "reason"}
_scope: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "boxlog_scope", default=None)
_last: dict[str, dict] = {}      # box id -> its newest event (the row's last_event)
_last_loaded = False
_up: set[str] = set()            # non-shared boxes whose box_up was seen
_noted: dict[str, object] = {}   # box id -> the crash/error already recorded


def _now() -> str:
    """UTC, SQLite's datetime('now') shape, like every other created_at."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _clip(s, n: int = REASON_CHARS) -> str | None:
    if s is None:
        return None
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _bound_op(box) -> str | None:
    """The newest op id of a turn bound to this box, if any."""
    for op, bid in reversed(list(boxes.registry._op_box.items())):
        if bid == box.id:
            return op
    return None


def actor_for(box) -> str:
    sc = _scope.get()
    if sc and sc.get("actor"):
        return sc["actor"]
    op = _bound_op(box)
    return f"turn {op}" if op else "app"


# --- scopes -------------------------------------------------------------------

@contextlib.contextmanager
def by(actor: str | None, reason: str | None = None):
    """Name who (and why) for everything recorded inside, without recording
    anything itself. API routes wrap their action in by(<user name>)."""
    outer = _scope.get() or {}
    tok = _scope.set({"boxes": set(outer.get("boxes") or ()),
                      "actor": actor or outer.get("actor"),
                      "reason": reason or outer.get("reason")})
    try:
        yield
    finally:
        _scope.reset(tok)


@contextlib.asynccontextmanager
async def action(box, event: str, *, actor: str | None = None,
                 reason: str | None = None):
    """Record ONE `event` for everything done to `box` inside (the boots and
    teardowns in between are not recorded separately), or `error` if it
    raises. Nested inside an action on the same box it records nothing: the
    outer one names the event (the reaper's idle stop wraps a destroy)."""
    outer = _scope.get() or {}
    if box.id in (outer.get("boxes") or ()):
        yield
        return
    sc = {"boxes": set(outer.get("boxes") or ()) | {box.id},
          "actor": actor or outer.get("actor"),
          "reason": reason or outer.get("reason")}
    tok = _scope.set(sc)
    failed: BaseException | None = None
    try:
        yield
    except BaseException as e:
        failed = e
        raise
    finally:
        _scope.reset(tok)
        who = sc["actor"] or actor_for(box)
        if failed is None:
            await record(box, event, reason=sc["reason"], actor=who)
        elif not isinstance(failed, (asyncio.CancelledError, GeneratorExit)):
            await record(box, "error", actor=who,
                         reason=f"{event.replace('_', ' ')} failed: {failed}")


def _quiet(box) -> bool:
    sc = _scope.get()
    return bool(sc and box.id in (sc.get("boxes") or ()))


# --- recording ------------------------------------------------------------------

async def record(box, event: str, *, reason: str | None = None,
                 actor: str | None = None, detail: dict | None = None) -> dict:
    """Write one event (never raises: a history hiccup must not fail a boot)."""
    sc = _scope.get() or {}
    ev = {"box_id": box.id, "kind": box.kind, "project": box.project,
          "runtime": box.runtime, "event": event,
          "reason": _clip(reason if reason is not None else sc.get("reason")),
          "actor": _clip(actor or actor_for(box), 120),
          "detail": detail, "created_at": _now()}
    _last[box.id] = ev
    bus.publish(BUS_CHAN, {"type": "box_event", **ev})
    try:
        from ..db import get_db
        db = await get_db()
        try:
            cur = await db.execute(
                "INSERT INTO box_events (box_id, kind, project, runtime, event, reason, "
                "actor, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ev["box_id"], ev["kind"], ev["project"], ev["runtime"], event,
                 ev["reason"], ev["actor"],
                 json.dumps(detail) if detail is not None else None, ev["created_at"]))
            ev["id"] = cur.lastrowid
            if cur.lastrowid and cur.lastrowid > KEEP_ROWS:
                await db.execute("DELETE FROM box_events WHERE id <= ?",
                                 (cur.lastrowid - KEEP_ROWS,))
            await db.commit()
        finally:
            await db.close()
    except Exception as e:  # noqa: BLE001 — the history is advice, never a failure
        print(f"[boxes] {box.id} {event} not recorded: {e}")
    return ev


async def happened(box, event: str, reason: str | None = None) -> None:
    """A boot or teardown as it happens (controllers, box_up/box_down). Not
    recorded inside an action on the same box: the action names the event."""
    if not _quiet(box):
        await record(box, event, reason=reason)


async def on_emit(event: str, box) -> None:
    """boxes._emit: box_up (network ready, guest about to boot) = started;
    box_down after a box_up = stopped. The shared box emits neither; its
    GuestVM records its own boots and teardowns."""
    try:
        if event == "box_up":
            _up.add(box.id)
            _noted.pop(box.id, None)
            await happened(box, "started")
        elif event == "box_down" and box.id in _up:
            _up.discard(box.id)
            await happened(box, "stopped")
    except Exception:  # noqa: BLE001 — never fail a box_up over the history
        pass


async def watch(box) -> None:
    """Record a crash or a failed boot nobody else reported: a QEMU that
    exited on its own, a docker box in state `failed`, a container that died
    under a running box. Once per occurrence. Cheap; the reaper calls it."""
    ctl = boxes.controller(box) if box.is_shared else box.ctl
    if ctl is None:
        return
    msg, key = None, None
    proc = getattr(ctl, "_proc", None)
    rc = getattr(proc, "returncode", None) if proc is not None else None
    if proc is not None and rc is not None:
        msg, key, ev = f"the guest exited on its own (QEMU exit code {rc})", ("rc", id(proc)), "crashed"
    elif getattr(ctl, "state", None) == "failed" and getattr(ctl, "error", None):
        msg, key, ev = str(ctl.error), ("err", ctl.error), "error"
    elif getattr(ctl, "state", None) == "running" and hasattr(ctl, "crashed"):
        # a container that died under a box that says running: the controller
        # moves the box to failed (its row stops claiming a live box)
        tok = _scope.set({"boxes": {box.id}, "actor": "app", "reason": None})
        try:                     # quiet: the cleanup's box_down is not a 'stopped'
            msg = await ctl.crashed()
        finally:
            _scope.reset(tok)
        if msg:
            _noted[box.id] = ("err", msg)     # the failed state is not a second event
            await record(box, "crashed", reason=msg, actor="app")
        return
    if msg is None or _noted.get(box.id) == key:
        return
    _noted[box.id] = key
    await record(box, ev, reason=msg, actor="app")


async def watch_all() -> None:
    for b in list(boxes.all_boxes()):
        try:
            await watch(b)
        except Exception:  # noqa: BLE001 — one odd box must not stop the rest
            pass


# --- reading ----------------------------------------------------------------------

def _row(r) -> dict:
    d = dict(r)
    if d.get("detail"):
        try:
            d["detail"] = json.loads(d["detail"])
        except ValueError:
            pass
    return d


async def events(box_id: str, limit: int = 50, before: int | None = None) -> list[dict]:
    """A box's history, newest first (a destroyed box's history still reads)."""
    from ..db import get_db
    limit = max(1, min(int(limit), 500))
    db = await get_db()
    try:
        q = ("SELECT id, box_id, kind, project, runtime, event, reason, actor, detail, "
             "created_at FROM box_events WHERE box_id = ?")
        args: list = [box_id]
        if before:
            q += " AND id < ?"
            args.append(int(before))
        async with db.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)) as cur:
            return [_row(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def last_events() -> dict[str, dict]:
    """box id -> newest event: from memory, seeded once from the table so the
    rows say "stopped 2h ago" across an app restart."""
    global _last_loaded
    if not _last_loaded:
        _last_loaded = True
        try:
            from ..db import get_db
            db = await get_db()
            try:
                async with db.execute(
                        "SELECT e.* FROM box_events e JOIN (SELECT box_id, MAX(id) AS m "
                        "FROM box_events GROUP BY box_id) x ON e.id = x.m") as cur:
                    for r in await cur.fetchall():
                        _last.setdefault(r["box_id"], _row(r))
            finally:
                await db.close()
        except Exception:  # noqa: BLE001 — no table yet: memory only
            pass
    return dict(_last)


# --- what each turn is doing now --------------------------------------------------

_tools: dict[str, dict] = {}     # event channel -> {"name", "detail", "since"}
_tap = None
_ARG_KEYS = ("command", "cmd", "path", "url", "query", "q", "file", "name", "task")


def _arg_detail(args) -> str | None:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return _clip(args, 70)
    if not isinstance(args, dict):
        return None
    for k in _ARG_KEYS:
        v = args.get(k)
        if isinstance(v, (list, tuple)):
            v = " ".join(map(str, v))
        if isinstance(v, str) and v.strip():
            return _clip(v, 70)
    return None


def _track(channel: str, ev: dict):
    t = ev.get("type") if isinstance(ev, dict) else None
    if t == "tool":
        _tools[channel] = {"name": str(ev.get("name") or "?"),
                           "detail": _arg_detail(ev.get("args")), "since": time.time()}
    elif t in ("tool_result", "final", "error", "job_end", "start"):
        _tools.pop(channel, None)
    return None                  # a tracker: nothing is queued


def start_tracking() -> None:
    """Idempotent. The tap's queue never fills: _track always answers None."""
    global _tap
    if _tap is None:
        _tap = bus.tap(_track)


def current_tool(channel: str | None) -> dict | None:
    return dict(_tools[channel]) if channel and channel in _tools else None


def turns_by_box() -> dict[str, list[dict]]:
    """box id -> the turns running in it now: op id, conversation, project,
    and the tool running (if the turn's channel said). Host-side registries
    only (broker envelopes, the op->box binding): nothing the guest says."""
    from . import broker
    out: dict[str, list[dict]] = {}
    for env in broker.live_turns():
        bid = boxes.op_box(env.op_id) or boxes.SHARED_ID
        out.setdefault(bid, []).append({
            "op_id": env.op_id, "conversation_id": env.conversation_id,
            "project": env.active_project, "ephemeral": bool(env.ephemeral),
            "tool": current_tool(env.event_chan)})
    return out


def reset() -> None:
    """Tests only."""
    global _last_loaded
    _last.clear()
    _up.clear()
    _noted.clear()
    _tools.clear()
    _last_loaded = False
