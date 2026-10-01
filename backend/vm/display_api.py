"""The live desktop of a box, in the web app (P1: watch only).

  GET  /api/vm/boxes/{id}/display      status: can this box show a desktop, is it
                                       up, what would starting it cost
  POST /api/vm/boxes/{id}/display      the operator's explicit start: boots the box
                                       if it is stopped (RAM budget permitting) and
                                       starts the display in the guest
  WS   /api/vm/boxes/{id}/display/ws   a noVNC session (subprotocol `binary`):
                                       cookie-authed, same-origin gated, pins the
                                       box for as long as it is open

Nothing here starts a box by itself: GET only reads and the WebSocket refuses a
stopped box. The guest side is guest/backend/display.py (vsock :5559, the same
newline-JSON hello, then raw RFB bytes); this module splices the browser to it.

Watch-only is enforced HERE, not trusted to the browser: every client byte goes
through RfbInputFilter, which drops key, pointer and clipboard messages unless the
`allow` callback says yes. P1 never does. P3 passes a callback that answers for
whoever holds control; nothing else in this file changes.

The desktop is a KVM layer: there is no Docker image of it (docs/docker-runtime.md),
so a Docker box says so instead of failing later.
"""
import asyncio
import json
import struct
from collections import Counter
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, WebSocket

from .. import bus
from ..auth import COOKIE_NAME, require_user, user_from_token
from ..config import settings
from . import boxdesk, boxes, boxlog, images
from .lifecycle import VMError

router = APIRouter(prefix="/api/vm/boxes", tags=["vm-display"])

GEOMETRY = {"width": 1280, "height": 800}      # guest/backend/display.py
GUEST_START_WAIT_S = 30                        # the guest starts Xvnc inside this
# WebSocket close codes the panel reads (4xxx = application)
CLOSE_UNAUTH, CLOSE_REFUSED, CLOSE_PROTOCOL, CLOSE_GUEST = 4401, 4409, 4400, 4502

# box id -> viewers connected now (for the panel's "2 watching" and the events)
_viewers: dict[str, int] = {}


# --- the RFB client->guest filter ---------------------------------------------------

class RfbViolation(Exception):
    """The client sent something no RFB 3.x viewer sends (or negotiated a
    security type the guest does not offer): the session ends."""


# what each client message type is, for the policy (see RfbInputFilter.allow)
KEY, POINTER, CUT_TEXT, RESIZE, XVP = "key", "pointer", "cut_text", "resize", "xvp"
_NEVER = frozenset({RESIZE, XVP})         # no holder may resize the guest or power it off
_MAX_CUT_TEXT = 16 * 1024 * 1024
_MAX_ENCODINGS = 1024


def watch_only(kind: str) -> bool:
    """The P1 policy: no input of any kind."""
    return False


class RfbInputFilter:
    """Filters the CLIENT half of an RFB stream so only watching gets through.

    The stream is parsed, not pattern-matched: the version / security / init
    handshake (None security only, which is all the guest offers), then framed
    messages. KeyEvent (4), PointerEvent (5, with noVNC's extended-buttons form),
    ClientCutText (6, including the extended-clipboard form) and QEMU's extended
    key event (255/0) are dropped unless `allow(kind)` is true; SetDesktopSize
    (251) and the xvp power message (250) are dropped always. Everything a viewer
    needs to watch (pixel format, encodings, update requests, fences, continuous
    updates) passes. An unknown message type is a RfbViolation: failing closed,
    since its length is unknown.

    feed(data) -> the bytes to forward to the guest (possibly none). It keeps the
    partial message across calls. `dropped` counts what was dropped, by kind."""

    def __init__(self, allow: Callable[[str], bool] = watch_only):
        self.allow = allow
        self.dropped: Counter = Counter()
        self._buf = bytearray()
        self._stage = "version"
        self._skip = 0

    # -- handshake ------------------------------------------------------------
    def _handshake(self) -> bytes:
        b = self._buf
        if self._stage == "version":
            if len(b) < 12:
                return b""
            if not (bytes(b[:8]) == b"RFB 003." and b[11:12] == b"\n"
                    and bytes(b[8:11]) in (b"003", b"007", b"008")):
                raise RfbViolation("not an RFB 3.3 / 3.7 / 3.8 viewer")
            out = bytes(b[:12])
            self._minor = int(bytes(b[8:11]))
            del b[:12]
            self._stage = "security" if self._minor >= 7 else "init"
            return out
        if self._stage == "security":
            if not b:
                return b""
            if b[0] != 1:                      # 1 = None, the only type offered
                raise RfbViolation("the desktop takes no security type but None")
            out = bytes(b[:1])
            del b[:1]
            self._stage = "init"
            return out
        if self._stage == "init":              # ClientInit: the shared flag
            if not b:
                return b""
            out = bytes(b[:1])
            del b[:1]
            self._stage = "messages"
            return out
        return b""

    # -- messages ---------------------------------------------------------------
    @staticmethod
    def _frame(b: bytearray):
        """(total length, kind) of the message at the start of `b`, or None when
        its header is not all here yet. kind is None for a message that passes."""
        t = b[0]
        if t == 0:
            return 20, None
        if t == 3 or t == 150:
            return 10, None
        if t == 2:
            if len(b) < 4:
                return None
            n = struct.unpack(">H", b[2:4])[0]
            if n > _MAX_ENCODINGS:
                raise RfbViolation("SetEncodings with an absurd length")
            return 4 + 4 * n, None
        if t == 4:
            return 8, KEY
        if t == 5:
            if len(b) < 2:
                return None
            return (7 if b[1] & 0x80 else 6), POINTER      # 0x80: extended buttons
        if t == 6:
            if len(b) < 8:
                return None
            n = abs(struct.unpack(">i", b[4:8])[0])       # negative: extended clipboard
            if n > _MAX_CUT_TEXT:
                raise RfbViolation("ClientCutText with an absurd length")
            return 8 + n, CUT_TEXT
        if t == 248:                                       # ClientFence
            if len(b) < 9:
                return None
            return 9 + b[8], None
        if t == 250:
            return 4, XVP
        if t == 251:
            if len(b) < 8:
                return None
            return 8 + 16 * b[6], RESIZE
        if t == 255:                                       # QEMU extension
            if len(b) < 2:
                return None
            if b[1] == 0:
                return 12, KEY                             # extended key event
            raise RfbViolation("an unknown QEMU extension message")
        raise RfbViolation(f"unknown client message type {t}")

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        out = bytearray()
        while True:
            if self._stage != "messages":
                got = self._handshake()
                if not got:
                    break                              # need more handshake bytes
                out += got
                continue
            if self._skip:                             # the rest of a dropped message
                n = min(self._skip, len(self._buf))
                del self._buf[:n]
                self._skip -= n
                if self._skip:
                    break
            if not self._buf:
                break
            fr = self._frame(self._buf)
            if fr is None:
                break
            total, kind = fr
            drop = kind is not None and (kind in _NEVER or not self.allow(kind))
            if not drop:
                if len(self._buf) < total:
                    break                              # a passing message arrives whole
                out += self._buf[:total]
                del self._buf[:total]
                continue
            self.dropped[kind] += 1
            have = min(total, len(self._buf))
            del self._buf[:have]
            self._skip = total - have                  # a big clipboard is skipped, not held
            if self._skip:
                break
        return bytes(out)


# --- what this box can do --------------------------------------------------------

def _ctl(box):
    return boxes.controller(box) if box.is_shared else box.ctl


def _running(box) -> bool:
    ctl = _ctl(box)
    return bool(ctl and ctl.running())


def free_ram_mb(box) -> int:
    """What the RAM budget has left for `box` to run: the cap less what the
    OTHER running boxes cost (a stopped box's reservation is its own)."""
    others = sum(boxes.ram_cost(b.mem_mb, b.runtime) for b in boxes.all_boxes()
                 if b.id != box.id and _running(b))
    return max(0, settings.vm_guest_ram_budget_mb - others)


async def _is_desktop_image(variant: str) -> bool:
    if variant == "desktop":
        return True
    from ..db import get_db
    db = await get_db()
    try:
        return variant in await images.descendants(db, "desktop")
    finally:
        await db.close()


def _variant(box) -> str:
    return (box.booted_image[0] if _running(box) and box.booted_image else box.image[0])


async def unsupported(box) -> str | None:
    """Why this box cannot show a desktop, or None when it can."""
    if box.runtime == "docker":
        return ("a Docker box has no desktop: the desktop image is a KVM layer. Give the "
                "project a KVM box with the `desktop` image (Runs in)")
    variant = _variant(box)
    if not await _is_desktop_image(variant):
        return (f"box {box.id} runs the `{variant}` image, which has no desktop. Give the "
                "project the `desktop` image (Runs in), or one built from it")
    return None


async def _guest_call(box, mode: str, timeout: float = 10.0) -> dict:
    """One display-listener request/response. ConnectionError when the guest
    does not answer on the port (a box started before the desktop shipped)."""
    loop = asyncio.get_running_loop()
    last = None
    for _ in range(6):
        try:
            sock = await asyncio.wait_for(box.transport.connect(boxes.PORT_DISPLAY), 5)
            break
        except (OSError, asyncio.TimeoutError) as e:
            last = e
            await asyncio.sleep(0.5)
    else:
        raise ConnectionError(str(last) or "no answer")
    try:
        await loop.sock_sendall(sock, json.dumps({"mode": mode}).encode() + b"\n")
        line, _ = await asyncio.wait_for(_read_line(loop, sock), timeout)
        return json.loads(line) if line else {}
    except (asyncio.TimeoutError, ValueError) as e:
        raise ConnectionError(f"bad answer from the display listener: {e or 'timed out'}")
    finally:
        sock.close()


async def _read_line(loop, sock) -> tuple[bytes, bytes]:
    """(the first line, whatever followed it in the same reads)."""
    buf = b""
    while b"\n" not in buf:
        if len(buf) > 65536:
            raise ValueError("no end of line")
        chunk = await loop.sock_recv(sock, 65536)
        if not chunk:
            break
        buf += chunk
    line, _, rest = buf.partition(b"\n")
    return line, rest


async def status(box, *, ask_guest: bool = True) -> dict:
    """The panel's whole picture of this box's desktop."""
    why = await unsupported(box)
    running = _running(box)
    need = 0 if running else boxes.ram_cost(box.mem_mb, box.runtime)
    free = free_ram_mb(box)
    out = {"box_id": box.id, "runtime": box.runtime, "image": _variant(box),
           "supported": why is None, "reason": why,
           "state": "running" if running else "stopped",
           "session": "off" if running else "stopped",
           "viewers": _viewers.get(box.id, 0), "need_mb": need, "free_mb": free,
           "fits": need <= free, "geometry": GEOMETRY, "watch_only": True,
           "guest": None, "note": None, "desk": boxdesk.state(box.id)}
    if why is None and running and ask_guest:
        try:
            g = await _guest_call(box, "status", timeout=4)
            out["guest"] = g
            if not g.get("installed", True):
                out["session"] = "missing"
                out["note"] = ("this box's image has no desktop yet: rebuild the `desktop` "
                               "image, then restart the box")
            else:
                out["session"] = "running" if g.get("running") else "off"
                if g.get("running"):
                    boxdesk.sync(box)          # the agent's seat (P2), if it has none yet
                    out["desk"] = boxdesk.state(box.id)
        except ConnectionError:
            out["session"] = "unavailable"
            out["note"] = ("the box does not answer on the desktop port: it was started "
                           "before the desktop shipped. Restart it")
    return out


def _box_or_404(box_id: str):
    b = boxes.get(box_id)
    if b is None:
        raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
    return b


@router.get("/{box_id}/display")
async def display_status(box_id: str, user: dict = Depends(require_user)):
    return await status(_box_or_404(box_id))


@router.post("/{box_id}/display")
async def display_start(box_id: str, user: dict = Depends(require_user)):
    """The operator's explicit start. Boots the box when it is stopped, but only
    when the RAM budget has room for it; then starts the display."""
    box = _box_or_404(box_id)
    why = await unsupported(box)
    if why:
        raise HTTPException(status_code=409, detail=why)
    if not _running(box):
        need, free = boxes.ram_cost(box.mem_mb, box.runtime), free_ram_mb(box)
        if need > free:
            raise HTTPException(
                status_code=409,
                detail=f"the desktop box needs {need} MB, {free} MB free. Stop another "
                       "running box first (Network → VMs)")
        try:
            with boxlog.by(f"operator {user.get('username') or '?'}", "operator desktop start"):
                await boxes.start(box)
        except (VMError, boxes.BoxError) as e:
            raise HTTPException(status_code=502, detail=str(e))
    try:
        g = await _guest_call(box, "start", timeout=GUEST_START_WAIT_S)
    except ConnectionError as e:
        raise HTTPException(
            status_code=502,
            detail="the box does not answer on the desktop port (it was started before "
                   f"the desktop shipped: restart it): {e}")
    if not g.get("ok"):
        raise HTTPException(status_code=409, detail=g.get("error") or "the desktop did not start")
    bus.publish(boxes.BUS_CHAN, {"type": "display", "box_id": box.id, "state": "running"})
    try:
        await boxdesk.ensure(box)              # the agent can drive it from here (P2)
    except (boxdesk.BoxDeskError, OSError, asyncio.TimeoutError):
        pass                                   # status()["desk"]["error"] says why
    return await status(box)


# --- the WebSocket splice ----------------------------------------------------------

def _publish_viewers(box_id: str) -> None:
    bus.publish(boxes.BUS_CHAN, {"type": "display", "box_id": box_id,
                                 "viewers": _viewers.get(box_id, 0)})


async def _refuse(ws: WebSocket, code: int, reason: str) -> None:
    """Accept, then close with a reason: a handshake rejection reaches the page
    as a bare 1006 with nothing to show."""
    await ws.accept(subprotocol="binary" if "binary" in (ws.scope.get("subprotocols") or ())
                    else None)
    await ws.close(code=code, reason=reason[:120])


@router.websocket("/{box_id}/display/ws")
async def display_ws(ws: WebSocket, box_id: str):
    # WebSocket can't use Depends(require_user); validate the session cookie
    # (the same-origin gate for the handshake is auth.SameOriginMiddleware)
    if user_from_token(ws.cookies.get(COOKIE_NAME)) is None:
        await ws.close(code=CLOSE_UNAUTH)
        return
    box = boxes.get(box_id)
    if box is None:
        await _refuse(ws, CLOSE_REFUSED, f"no box {box_id}")
        return
    why = await unsupported(box)
    if why:
        await _refuse(ws, CLOSE_REFUSED, why)
        return
    if not _running(box):                      # never boot a box from a viewer
        await _refuse(ws, CLOSE_REFUSED, "the box is stopped: start the desktop first")
        return
    await splice(ws, box)


async def splice(ws: WebSocket, box, allow: Callable[[str], bool] = watch_only) -> None:
    """Pin the box, dial its display listener, ask for an RFB session and relay
    bytes: guest -> browser untouched, browser -> guest through RfbInputFilter."""
    loop = asyncio.get_running_loop()
    ctl = boxes.controller(box)
    try:
        await ctl.acquire()                    # already running: this only pins it
    except VMError as e:
        await _refuse(ws, CLOSE_REFUSED, str(e))
        return
    sock = None
    counted = False
    try:
        try:
            sock = await asyncio.wait_for(box.transport.connect(boxes.PORT_DISPLAY), 8)
            await loop.sock_sendall(sock, b'{"mode":"rfb"}\n')
            line, rest = await asyncio.wait_for(_read_line(loop, sock), GUEST_START_WAIT_S)
            ack = json.loads(line or b"{}")
        except (OSError, asyncio.TimeoutError, ValueError):
            await _refuse(ws, CLOSE_GUEST, "the box does not answer on the desktop port: "
                          "restart it")
            return
        if not ack.get("ok"):
            await _refuse(ws, CLOSE_GUEST, str(ack.get("error") or "the desktop did not start"))
            return
        await ws.accept(subprotocol="binary" if "binary" in (ws.scope.get("subprotocols") or ())
                        else None)
        _viewers[box.id] = _viewers.get(box.id, 0) + 1
        counted = True
        _publish_viewers(box.id)
        flt = RfbInputFilter(allow)
        reason = await _relay(ws, loop, sock, rest, flt)
        try:
            await ws.close(code=1000 if reason is None else CLOSE_PROTOCOL,
                           reason=(reason or "")[:120])
        except RuntimeError:
            pass                               # already closed by the browser
    finally:
        if counted:
            _viewers[box.id] = max(0, _viewers.get(box.id, 1) - 1)
            if not _viewers[box.id]:
                _viewers.pop(box.id, None)
            _publish_viewers(box.id)
        if sock is not None:
            sock.close()
        ctl.release()


async def _relay(ws: WebSocket, loop, sock, first: bytes, flt: RfbInputFilter) -> str | None:
    """Until either side hangs up. None for a normal end, else why it stopped."""
    async def to_browser():
        if first:
            await ws.send_bytes(first)
        while True:
            data = await loop.sock_recv(sock, 65536)
            if not data:
                return None
            await ws.send_bytes(data)

    async def to_guest():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return None
            data = msg.get("bytes")
            if data is None:                   # a text frame is not RFB
                continue
            try:
                out = flt.feed(data)
            except RfbViolation as e:
                return f"protocol error: {e}"
            if out:
                await loop.sock_sendall(sock, out)

    tasks = {asyncio.ensure_future(to_browser()), asyncio.ensure_future(to_guest())}
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
    for t in done:
        try:
            r = t.result()
        except Exception:  # noqa: BLE001 -- a hung-up socket on either side ends it
            r = None
        if r:
            return r
    return None
