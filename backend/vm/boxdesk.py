"""A desktop box's screen as a computer-use desk (the live desktop, step P2).

The agent drives the box's desktop (display :100, guest/backend/display.py)
with the SAME desk tool it uses on the operator's computer, as
`desk(action=..., computer="sandbox")`. Nothing new is offered to the model:
this module only makes the box look, to backend/desk.py, like a computer that
connected to /api/desk/ws.

    desk tool -> desk.act() -> grants, fresh-frame rule, rate limits, audit, taint
              -> Desk.ws (BoxWS, below) -> a JSON line on the guest's display
                 socket (:5559, {"mode":"desk"}) -> deskbox.py -> jav3-desk's
                 Session -> xdotool / maim on :100
    and back: the reader task feeds every frame to desk.on_frame().

- Registration: ensure(box) dials the guest, waits for its hello and calls
  desk.attach(box_id=...). It runs when the operator starts the desktop
  (display_api.display_start) and whenever a status read finds the screen up
  without a registered desk (sync), so a host restart heals on the next look.
  The connection lives exactly as long as the screen: the guest ends it when
  the display stops, the reader then detaches the desk and the tools vanish.
- Identity: a box desk has a `device_tokens` row (scope desk, name box:<id>,
  its secret thrown away at once: nothing can connect with it, and it never
  expires), so grants, the audit (desk_actions) and Settings -> Access ->
  Computer use work unchanged. Grants start at screen ON, input ON, shell OFF
  (run_code is the box's shell anyway); once the row exists the operator's
  choices stand (a Stop is not undone by the next registration).
- Scope: Desk.box_id. Only turns running in that box see it, and those turns
  see no other computer (desk._pool).
"""
import asyncio
import hashlib
import json
import secrets
import socket
import time

from .. import bus, desk
from ..db import get_db
from . import boxes

CONNECT_WAIT_S = 5
HELLO_WAIT_S = 10
LINE_MAX = 8 << 20                  # above desk.IMAGE_B64_CAP, which is checked per image
RETRY_AFTER_S = 30                  # sync() does not redial a box that just failed
FAR_FUTURE = "9999-12-31 00:00:00"  # the row never lapses: it is not a credential
DEFAULT_GRANTS = (1, 1, "off")      # screen, input, shell


class BoxDeskError(Exception):
    """The desktop's agent seat could not be reached; the message says why."""


def name_for(box_id: str) -> str:
    return f"box:{box_id}"


class BoxWS:
    """What desk.Desk calls `ws`: send_text writes a line to the guest."""

    def __init__(self, sock):
        self.sock = sock
        self.closed = False

    async def send_text(self, text: str) -> None:
        if self.closed:
            raise desk.DeskError("the desktop connection is closed")
        try:
            await asyncio.get_running_loop().sock_sendall(self.sock, text.encode() + b"\n")
        except OSError as e:
            raise desk.DeskError(f"the desktop connection broke ({e or 'closed'})")

    async def close(self, code: int = 1000) -> None:
        """Hang up. The socket is shut down, not closed: the reader task is
        waiting on it and wakes to end of stream, then closes it (_read)."""
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _Lines:
    """Newline-delimited frames off a non-blocking socket."""

    def __init__(self, sock, buf: bytes = b""):
        self.sock, self.buf = sock, buf

    async def next(self, timeout: float) -> bytes | None:
        """The next non-empty line; None at end of stream. TimeoutError when
        nothing arrives in `timeout`; ValueError past LINE_MAX."""
        loop = asyncio.get_running_loop()
        end = time.monotonic() + timeout
        while True:
            while b"\n" in self.buf:
                line, _, self.buf = self.buf.partition(b"\n")
                if line.strip():
                    return line
            if len(self.buf) > LINE_MAX:
                raise ValueError("a frame over the size limit")
            left = end - time.monotonic()
            if left <= 0:
                raise asyncio.TimeoutError()
            data = await asyncio.wait_for(loop.sock_recv(self.sock, 262144), left)
            if not data:
                return None
            self.buf += data


# --- the box's device row ---------------------------------------------------------------

async def device_id(box_id: str) -> int:
    """The desk token row of this box's desktop, made on first use with the
    default grants. An existing row's grants are never touched."""
    name = name_for(box_id)
    db = await get_db()
    try:
        async with db.execute(
                "SELECT id FROM device_tokens WHERE name = ? AND scope = 'desk' "
                "AND paired_by = 'box' AND revoked = 0 ORDER BY id DESC LIMIT 1",
                (name,)) as cur:
            row = await cur.fetchone()
        if row is not None:
            did = row["id"]
        else:
            secret = hashlib.sha256(secrets.token_bytes(32)).hexdigest()   # never kept
            cur = await db.execute(
                "INSERT INTO device_tokens (name, token_hash, hostname, platform, "
                "paired_by, expires_at, scope) VALUES (?,?,?,?,?,?, 'desk')",
                (name, hashlib.sha256(secret.encode()).hexdigest(), box_id, "linux",
                 "box", FAR_FUTURE))
            did = cur.lastrowid
        await db.execute(
            "INSERT INTO desk_grants (device_id, screen, input, shell) VALUES (?,?,?,?) "
            "ON CONFLICT(device_id) DO NOTHING", (did, *DEFAULT_GRANTS))
        await db.commit()
        return did
    finally:
        await db.close()


# --- the connection ---------------------------------------------------------------------

_live: dict[str, tuple] = {}            # box id -> (Desk, reader task)
_locks: dict[str, asyncio.Lock] = {}
_failed: dict[str, tuple[float, str]] = {}   # box id -> (monotonic, why)
_syncing: set[str] = set()


class _Control:
    """The operator holds a box's desktop (live desktop P3). The counts are all
    that is ever kept of what they did: what was typed may be a password."""

    def __init__(self, viewer: str, by: str):
        self.viewer, self.by = viewer, by
        self.since = time.monotonic()
        self.keys = self.pointers = 0
        self.grace: asyncio.Task | None = None      # runs while the holder's window is gone


_control: dict[str, _Control] = {}                  # box id -> who holds it (absent = the agent)
# (box id, viewer id) -> a "let go of held keys" coroutine function per live socket
_socks: dict[tuple[str, str], list] = {}
_last_pub: dict[str, float] = {}


def reset_for_tests() -> None:
    for _, task in _live.values():
        task.cancel()
    _live.clear()
    _locks.clear()
    _failed.clear()
    _syncing.clear()
    for c in _control.values():
        if c.grace is not None:
            c.grace.cancel()
    _control.clear()
    _socks.clear()
    _last_pub.clear()


def live(box_id: str):
    """The registered Desk of this box, or None."""
    cur = _live.get(box_id)
    if cur is not None and not cur[1].done() and desk._desks.get(cur[0].device_id) is cur[0]:
        return cur[0]
    return None


def state(box_id: str) -> dict:
    """What a status read tells the panel about the agent's seat."""
    d = live(box_id)
    err = _failed.get(box_id)
    return {"connected": d is not None, "device_id": d.device_id if d else None,
            "error": err[1] if err and d is None else None}


async def _dial(box) -> tuple:
    """Connect to the guest's display listener in desk mode: (socket, _Lines)
    positioned after the ack line."""
    try:
        sock = await asyncio.wait_for(box.transport.connect(boxes.PORT_DISPLAY),
                                      CONNECT_WAIT_S)
    except (OSError, asyncio.TimeoutError) as e:
        raise BoxDeskError(f"the box does not answer on the desktop port: {e or 'timed out'}")
    try:
        await asyncio.get_running_loop().sock_sendall(sock, b'{"mode": "desk"}\n')
        lines = _Lines(sock)
        ack = await lines.next(HELLO_WAIT_S)
        try:
            reply = json.loads(ack or b"{}")
        except ValueError:
            reply = {}
        if not isinstance(reply, dict) or not reply.get("ok"):
            raise BoxDeskError((reply.get("error") if isinstance(reply, dict) else None)
                               or "the desktop did not accept the agent's seat")
        return sock, lines
    except BaseException:
        sock.close()
        raise


async def _hello(lines: _Lines) -> dict:
    try:
        while True:
            line = await lines.next(HELLO_WAIT_S)
            if line is None:
                raise BoxDeskError("the desktop's desk client exited before it said hello")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("type") == "error":
                raise BoxDeskError(str(msg.get("error") or "the desk client failed")[:300])
            if isinstance(msg, dict) and msg.get("type") == "hello":
                return msg
    except asyncio.TimeoutError:
        raise BoxDeskError("the desktop's desk client did not say hello")
    except ValueError as e:
        raise BoxDeskError(str(e))


async def ensure(box) -> desk.Desk:
    """Register the box's desktop as a desk (idempotent). The screen must be
    up: the guest refuses otherwise and the message comes back as BoxDeskError."""
    lock = _locks.setdefault(box.id, asyncio.Lock())
    async with lock:
        d = live(box.id)
        if d is not None:
            return d
        try:
            did = await device_id(box.id)
            sock, lines = await _dial(box)
            try:
                hello = await _hello(lines)
            except BaseException:
                sock.close()
                raise
        except BoxDeskError as e:
            _failed[box.id] = (time.monotonic(), str(e))
            raise
        _failed.pop(box.id, None)
        held = _control.get(box.id)        # a seat that registers while the operator is at the screen
        d = await desk.attach(did, name_for(box.id), BoxWS(sock), hello, "", box_id=box.id,
                              operator_since=held.since if held else None)
        _live[box.id] = (d, asyncio.ensure_future(_read(box.id, d, lines)))
        return d


async def ensure_quietly(box) -> desk.Desk | None:
    """ensure() for callers that must not fail because the agent's seat did:
    the reason lands in state()["error"] instead."""
    try:
        return await ensure(box)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — the operator's start / status read goes on
        _failed[box.id] = (time.monotonic(), str(e) or type(e).__name__)
        return None


async def _read(box_id: str, d: desk.Desk, lines: _Lines) -> None:
    """The guest's frames into desk.on_frame, until the connection ends: the
    screen stopped, the box stopped, a Stop, or the client went silent."""
    why = "disconnected"
    try:
        while True:
            line = await lines.next(desk.IDLE_DROP_S)
            if line is None:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            reply = desk.on_frame(d, msg)
            if reply is not None:
                await d.send(reply)
    except asyncio.TimeoutError:
        why = "went silent"
    except (OSError, ValueError, desk.DeskError):
        pass
    finally:
        await desk.detach(d, why)
        await d.ws.close()
        d.ws.sock.close()
        if _live.get(box_id, (None,))[0] is d:
            _live.pop(box_id, None)


async def drop(box_id: str, why: str = "desktop stopped") -> None:
    """End the box's desk now (the box is going away)."""
    d = live(box_id)
    if d is not None:
        await desk.disconnect(d.device_id, why)


def sync(box) -> None:
    """The screen is up (a status read saw it): make sure the agent has its
    seat. Fire and forget, once at a time per box, and not again for
    RETRY_AFTER_S after a failure, so a poll never turns into a retry storm."""
    if live(box.id) is not None or box.id in _syncing:
        return
    bad = _failed.get(box.id)
    if bad is not None and time.monotonic() - bad[0] < RETRY_AFTER_S:
        return
    _syncing.add(box.id)

    async def go():
        try:
            await ensure_quietly(box)      # a failure is recorded; state() reports it
        finally:
            _syncing.discard(box.id)

    asyncio.ensure_future(go())


# --- the operator takes the desktop, then hands it back (live desktop P3) --------------
#
# Control is `agent` (the default) or `operator`. The first click in the viewer
# takes it (display_api's POST .../display/control, naming its own viewer id);
# [Hand back], or that window staying gone for GRACE_S, returns it. There is no idle
# hand-back: the operator decided that only those two end a take-over.
#
# Three locks, so a bug in one does not put two pairs of hands on the screen:
#   1. desk.act refuses input verbs while Desk.operator_since is set (screenshots
#      stay allowed, so the agent can watch);
#   2. the guest seat is told input is off (desk.hold_for_operator), WITHOUT writing
#      the grants row: a host restart in the middle leaves the agent's grants as the
#      operator set them, not locked out;
#   3. display_api's RFB filter admits key and pointer only from the socket of the
#      holder (admit(), below), and never the clipboard.

GRACE_S = 10                # the holder's window may come back inside this and keep control
ACTIVITY_GAP_S = 3          # the window hears "the agent just acted" at most this often
TURNS_FRESH_S = 120         # a conversation that acted this recently is driving


class ControlError(Exception):
    """The take-over cannot happen; the message says why (the route's 409)."""


def control_state(box_id: str) -> dict:
    """What the window shows: who holds the desktop and, for the operator, which
    window (the viewer id its own noVNC session announced) and for how long."""
    c = _control.get(box_id)
    if c is None:
        return {"holder": "agent", "viewer": None, "by": None, "held_s": 0}
    return {"holder": "operator", "viewer": c.viewer, "by": c.by,
            "held_s": int(time.monotonic() - c.since)}


def _publish_control(box_id: str) -> None:
    bus.publish(boxes.BUS_CHAN, {"type": "display", "box_id": box_id,
                                 "control": control_state(box_id)})


def viewer_up(box_id: str, viewer: str, release) -> None:
    """A viewer socket opened. `release` is an async callable that lets go of
    whatever keys / buttons it holds on the guest. A holder whose window comes
    back inside the grace keeps control."""
    _socks.setdefault((box_id, viewer), []).append(release)
    c = _control.get(box_id)
    if c is not None and c.viewer == viewer and c.grace is not None:
        c.grace.cancel()
        c.grace = None


def viewer_down(box_id: str, viewer: str, release) -> None:
    """A viewer socket closed. When it was the holder's last one the grace
    clock starts; nothing else about control changes."""
    socks = _socks.get((box_id, viewer), [])
    if release in socks:
        socks.remove(release)
    if not socks:
        _socks.pop((box_id, viewer), None)
    c = _control.get(box_id)
    if c is not None and c.viewer == viewer and (box_id, viewer) not in _socks \
            and c.grace is None:
        c.grace = asyncio.ensure_future(_grace(box_id, c))


async def _grace(box_id: str, c: _Control) -> None:
    await asyncio.sleep(GRACE_S)
    if _control.get(box_id) is c:
        await hand_back(box_id, "the window disconnected")


def admit(box_id: str, viewer: str, kind: str) -> bool:
    """The RFB filter's question for a key or pointer message from `viewer`'s
    socket: yes only while that window holds control. Counts what it admits;
    the clipboard is never admitted, by anyone."""
    c = _control.get(box_id)
    if c is None or c.viewer != viewer:
        return False
    if kind == "key":
        c.keys += 1
    elif kind == "pointer":
        c.pointers += 1
    else:
        return False
    return True


async def _audit_control(box_id: str, params: dict, by: str) -> None:
    """A desk_actions row on the box's device: verb operator_control, counts only."""
    try:
        did = await device_id(box_id)
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO desk_actions (device_id, verb, params, ok, approver) "
                "VALUES (?,?,?,1,?)",
                (did, "operator_control", json.dumps(params)[:4000], (by or "operator")[:80]))
            await db.commit()
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — auditing never breaks the take-over
        pass


async def take(box, viewer: str, by: str = "") -> dict:
    """The operator clicked into the viewer named `viewer`. Idempotent for the
    holder's own window; another window must wait for a hand back."""
    if not viewer or (box.id, viewer) not in _socks:
        raise ControlError("that window is not connected to the desktop: reload it")
    c = _control.get(box.id)
    if c is not None:
        if c.viewer == viewer:
            return control_state(box.id)
        raise ControlError("another window holds control of this desktop: hand it back there first")
    c = _control[box.id] = _Control(viewer, by or "operator")
    d = live(box.id)
    if d is not None:
        await desk.hold_for_operator(d, c.since)
    await _audit_control(box.id, {"phase": "start", "box": box.id, "by": c.by}, c.by)
    await desk._event("desk_operator_control",
                      f"{c.by} took control of the desktop of box {box.id} (the agent is paused)",
                      severity="info", by_operator=True,
                      detail={"box_id": box.id, "device_id": d.device_id if d else None,
                              "phase": "start"})
    _publish_control(box.id)
    return control_state(box.id)


async def hand_back(box_id: str, why: str = "handed back") -> dict:
    """Control returns to the agent: held keys are let go, the seat hears its
    real grants, the agent's frame is dropped and its next result says how long
    the operator used the desktop. A no-op when the agent already has it."""
    c = _control.pop(box_id, None)
    if c is None:
        return control_state(box_id)
    if c.grace is not None and c.grace is not asyncio.current_task():
        c.grace.cancel()
    secs = time.monotonic() - c.since
    for release in list(_socks.get((box_id, c.viewer), ())):
        try:
            await release()
        except Exception:  # noqa: BLE001 — best effort: the socket may be gone
            pass
    d = live(box_id)
    if d is not None and d.operator_since is not None:
        await desk.release_to_agent(d, secs)
    await _audit_control(box_id, {"phase": "end", "box": box_id, "by": c.by, "why": why,
                                  "seconds": round(secs), "keys": c.keys,
                                  "pointers": c.pointers}, c.by)
    _publish_control(box_id)
    return control_state(box_id)


# --- "the agent is driving" ---------------------------------------------------------------

def _turns(d: desk.Desk) -> list[int]:
    now = time.monotonic()
    return sorted(cid for cid, at in d.turns.items() if now - at < TURNS_FRESH_S)


def agent_state(box_id: str) -> dict:
    """How long ago the agent last acted on this desktop (None: never since the
    seat registered) and the conversations driving it, for the window's label
    and its Stop button."""
    d = live(box_id)
    if d is None:
        return {"active_age_s": None, "turns": []}
    age = None if d.last_action_at is None else max(0, int(time.time() - d.last_action_at))
    return {"active_age_s": age, "turns": _turns(d)}


def _on_activity(d: desk.Desk) -> None:
    """desk.act just let the agent act on a box desktop: tell the window (it
    flips its own label back after a quiet spell, so nothing polls)."""
    now = time.monotonic()
    if now - _last_pub.get(d.box_id, -1e9) < ACTIVITY_GAP_S:
        return
    _last_pub[d.box_id] = now
    bus.publish(boxes.BUS_CHAN, {"type": "display", "box_id": d.box_id,
                                 "agent": {"active_age_s": 0, "turns": _turns(d)}})


desk.activity_hook = _on_activity
