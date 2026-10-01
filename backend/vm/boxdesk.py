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

from .. import desk
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


def reset_for_tests() -> None:
    for _, task in _live.values():
        task.cancel()
    _live.clear()
    _locks.clear()
    _failed.clear()
    _syncing.clear()


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
        d = await desk.attach(did, name_for(box.id), BoxWS(sock), hello, "", box_id=box.id)
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
