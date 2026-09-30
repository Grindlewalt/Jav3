"""Host-side AF_VSOCK model gateway — the only thing the guest can reach.

The guest has no network device. Its one path off-box is a vsock stream to the
host (CID 2) on settings.vm_vsock_port, over which it speaks newline-delimited
JSON. Each `model_call` request is metered on the host by the guest-supplied
op_id (registered here so `budget.get(op_id)` resolves it — the connection lands
on THIS server's task, not the turn's, so contextvar propagation wouldn't reach
it; the Phase-1 op_id keying is exactly what makes this work) and answered by
streaming `model.complete`'s events straight back. The DeepSeek key stays host-
side and never crosses the boundary.

Phase 2 handles `model_call` (+ `ping`). Phase 3 adds tool-broker ops here.
"""
import asyncio
import json
import re
import socket
import time

from ..agent import budget as budget_mod
from ..agent.budget import BudgetExceeded
from ..agent.model import ModelError, call_box_id, model
from ..config import settings
from .. import runtime
from . import boxes, broker, gateway_log

# kind -> fn(box) -> tar.gz bytes (WP3 registers "service", WP5 "builder").
# shared/project get the turn package (guest_pkg). docs/boxes-contract.md D.
_PACKAGE_BUILDERS: dict = {}
# op -> async fn(loop, conn, req, box): kind-specific report ops (svc_report,
# build_report) owned by WP3 / WP5. Gated by boxes.GATEWAY_OPS first.
_OP_HANDLERS: dict = {}


def register_package_builder(kind: str, fn) -> None:
    _PACKAGE_BUILDERS[kind] = fn


def register_op_handler(op: str, fn) -> None:
    _OP_HANDLERS[op] = fn


# --- hard caps on what the guest may make the host hold ----------------------
# The guest is the party this design does not trust, and every byte it sends is
# buffered here before any check can run on it. Each cap below answers the guest
# with an error naming it, closes the connection, and leaves a refusal row and a
# security event (gateway_log.record_cap_trip). The numbers are sized above the
# largest request a real turn makes: a model_call carries the whole context
# (about 1M tokens is ~4 MB of text, ~12 MB if it is all non-ASCII escapes) plus
# up to three screenshots of 4.5 MB (6 MB as base64); a tool_broker_call carries
# one tool call's arguments (a model's whole output is under 2 MB).
MAX_LINE_BYTES = 1 << 20                 # any request line, unless its op has more:
MAX_LINE_BYTES_BY_OP = {"model_call": 48 << 20, "tool_broker_call": 8 << 20}
BOX_BUFFER_BYTES = 128 << 20             # request bytes one box's connections hold at once
TOTAL_BUFFER_BYTES = 256 << 20           # ...and all boxes together
MAX_CONNS_PER_BOX = 256                  # 24 funnel nodes, some with a round of parallel tool calls
MAX_CONNS_TOTAL = 512
READ_IDLE_S = 120.0                      # a connection that sends no request in this long is closed
LINE_DEADLINE_S = 120.0                  # a request line must be complete this long after its first byte
SEND_TIMEOUT_S = 120.0                   # a guest that stops reading its reply is dropped
MAX_REFUSALS_PER_CONN = 16               # bad ops / bad tokens on one connection, then it is closed
PACKAGE_PER_MIN = 12                     # get_guest_package builds a tarball: boot asks once
_RECV_CHUNK = 1 << 18
_OP_PREFIX = re.compile(rb'\s*\{\s*"op"\s*:\s*"([a-z_]{1,32})"')


class _Refuse(Exception):
    """A cap tripped on this connection. `cap` is the error the guest gets and
    the name in the refusal log and the security event."""

    def __init__(self, cap: str, message: str):
        super().__init__(message)
        self.cap, self.message = cap, message


class _Meter:
    __slots__ = ("conns", "held")

    def __init__(self):
        self.conns = 0
        self.held = 0


_meters: dict[str, _Meter] = {}          # box key -> what its connections hold
_totals = _Meter()
_hits: dict[tuple, list] = {}            # (box key, what) -> recent timestamps


def _rate_ok(key: str, what: str, limit: int, per: float = 60.0) -> bool:
    now = time.monotonic()
    q = _hits.setdefault((key, what), [])
    q[:] = [t for t in q if now - t < per]
    if len(q) >= limit:
        return False
    q.append(now)
    return True


class _Claim:
    """One connection's claim on its box's caps: a connection slot, and the
    bytes its request buffer and the request being served hold. Released as a
    whole in close()."""

    def __init__(self, key: str):
        self.key = key
        self.m = _meters.setdefault(key, _Meter())
        self.held = 0
        self.open = False

    def acquire(self) -> None:
        if self.m.conns >= MAX_CONNS_PER_BOX or _totals.conns >= MAX_CONNS_TOTAL:
            raise _Refuse("too_many_connections",
                          f"at most {MAX_CONNS_PER_BOX} connections per box "
                          f"({MAX_CONNS_TOTAL} in all)")
        self.m.conns += 1
        _totals.conns += 1
        self.open = True

    def hold(self, n: int) -> None:
        d = n - self.held
        if d > 0 and (self.m.held + d > BOX_BUFFER_BYTES
                      or _totals.held + d > TOTAL_BUFFER_BYTES):
            raise _Refuse("buffer_budget",
                          f"this box's requests already hold {self.m.held >> 20} MB "
                          f"of the gateway's {BOX_BUFFER_BYTES >> 20} MB")
        self.held = n
        self.m.held += d
        _totals.held += d

    def close(self) -> None:
        if not self.open:
            return
        self.open = False
        self.m.held -= self.held
        _totals.held -= self.held
        self.held = 0
        self.m.conns -= 1
        _totals.conns -= 1
        if self.m.conns <= 0 and self.m.held <= 0 and _meters.get(self.key) is self.m:
            del _meters[self.key]


async def _send(loop, conn, obj: dict) -> None:
    data = (json.dumps(obj) + "\n").encode()
    try:
        await asyncio.wait_for(loop.sock_sendall(conn, data), SEND_TIMEOUT_S)
    except asyncio.TimeoutError:
        # the guest stopped reading: nothing more will reach it, and the model
        # call or tool result waiting to be sent must not wait forever
        raise ConnectionError("guest stopped reading its reply") from None


def _line_cap(head: bytes) -> int:
    """How long a request line may be, from the op it names (the guest's
    clients all write `{"op": ...` first; a line that does not is small)."""
    m = _OP_PREFIX.match(head[:128])
    return MAX_LINE_BYTES_BY_OP.get(m.group(1).decode(), MAX_LINE_BYTES) if m else MAX_LINE_BYTES


async def _read_line(loop, conn, buf: bytearray, claim: _Claim):
    """The next request line (without its newline), taken from `buf` and the
    socket; None when the guest closed or sat idle. Every byte read is charged
    to `claim`, a line over its op's cap or over the buffer budget raises
    _Refuse."""
    scanned, started = 0, None
    while True:
        i = buf.find(b"\n", scanned)
        if i >= 0:
            line = bytes(buf[:i])
            del buf[:i + 1]
            if len(line) > MAX_LINE_BYTES and len(line) > _line_cap(line):
                raise _Refuse("request_too_large", f"a {len(line) >> 20} MB request line")
            return line
        scanned = len(buf)
        if scanned > MAX_LINE_BYTES and scanned > _line_cap(bytes(buf[:128])):
            raise _Refuse("request_too_large", f"over {scanned >> 20} MB without a newline")
        if buf and started is None:
            started = loop.time()
        wait = (READ_IDLE_S if started is None
                else min(READ_IDLE_S, started + LINE_DEADLINE_S - loop.time()))
        if wait <= 0:
            raise _Refuse("request_timeout", f"a request line unfinished after {LINE_DEADLINE_S:.0f} s")
        try:
            chunk = await asyncio.wait_for(loop.sock_recv(conn, _RECV_CHUNK), wait)
        except asyncio.TimeoutError:
            if started is None:
                return None                  # idle: the fd goes back, nothing to report
            raise _Refuse("request_timeout",
                          f"a request line unfinished after {LINE_DEADLINE_S:.0f} s") from None
        if not chunk:
            return None
        buf.extend(chunk)
        claim.hold(len(buf))


async def _tracked(loop, conn, op_id: str, coro, pending: bytearray):
    """Run `coro` (a model stream or a brokered tool call) as a task the turn's
    stop can reach (broker.cancel_inflight), and watch the connection while it
    runs: a guest that hangs up cancels it too. Returns the task's result;
    None, after telling the guest, when the task was cancelled."""
    task = asyncio.ensure_future(coro)
    broker.track_inflight(op_id, task)
    watch = None
    try:
        while not task.done():
            watch = asyncio.ensure_future(loop.sock_recv(conn, 65536))
            await asyncio.wait({task, watch}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                break
            try:
                data = watch.result()
            except OSError:
                data = b""
            if not data:
                raise ConnectionError("guest hung up while its request was served")
            # a client that pipelines: keep what it sent for the read loop
            pending.extend(data)
            if len(pending) > MAX_LINE_BYTES:
                raise _Refuse("request_too_large", "input piled up while a request was served")
        if task.cancelled():
            await _send(loop, conn, {"type": "error", "error": "turn_stopped",
                                     "message": "this turn was stopped; its work was cancelled"})
            return None
        return task.result()
    finally:
        if watch is not None and not watch.done():
            watch.cancel()
        if not task.done():
            task.cancel()


def _entitled(req: dict) -> bool:
    """Whether this request may act as the op_id it names.

    Registration answers "is this a real turn"; it never answered "is this YOUR
    turn". op_ids are deterministic and guest-supplied, so guessing a live one
    was enough to borrow its whole envelope — its project pin, web session,
    artifact store and Budget. The per-turn token the host shipped in the turn
    spec is the missing half. Checked on every op that carries an op_id."""
    return broker.verify_token(req.get("op_id") or "", req.get("op_token"))


def _op_from_elsewhere(req: dict, box) -> bool:
    """Multi-box: an op_id bound to one box (guest_turn) must arrive from it.
    Tokens are per-turn secrets shipped only to that box, so this is a second
    lock, not the first: a token that leaked into another box still fails."""
    if box is None:
        return False
    bound = boxes.op_box(req.get("op_id") or "")
    return bound is not None and bound != box.id


async def _refused(req: dict, box, op_name, reason: str) -> None:
    """Leave one row in the refusal log (Security > Calls). The box and project
    are the host's own knowledge of the caller (its listener / CID, the bound
    turn), never something the guest claimed."""
    op_id = req.get("op_id")
    op_id = op_id if isinstance(op_id, str) else ""
    env = broker.get_turn(op_id) if op_id else None
    box_id = box.id if box is not None else (boxes.op_box(op_id) if op_id else None)
    project = (env.active_project if env is not None
               else (box.project if box is not None else None))
    await gateway_log.record_refusal(op_name, reason, box_id, project)


async def _handle_model_call(loop, conn, req: dict, box=None,
                             pending: bytearray | None = None) -> bool:
    """True when the request was refused (the connection counts those)."""
    op_id = req.get("op_id") or "vm-anon"
    entitled = _entitled(req)
    if not entitled or _op_from_elsewhere(req, box):
        await _refused(req, box, "model_call", "unknown_op_id" if not entitled else "wrong_box")
        await _send(loop, conn, {"type": "error", "error": "unknown_op_id",
                                 "message": f"op_id {op_id!r} is not this caller's turn"})
        return True
    # op_id pinning: only a turn the host already registered (guest_turn) may
    # spend. The gateway never opens a budget itself — a compromised guest can't
    # invent op_ids to escape the per-operation cap by rotating ids.
    if budget_mod.get(op_id) is None:
        await _refused(req, box, "model_call", "unknown_op_id")
        await _send(loop, conn, {"type": "error", "error": "unknown_op_id",
                                 "message": f"op_id {op_id!r} is not a registered turn"})
        return True
    await _tracked(loop, conn, op_id, _stream_model(loop, conn, req, op_id, box),
                   pending if pending is not None else bytearray())
    return False


async def _stream_model(loop, conn, req: dict, op_id: str, box) -> None:
    messages = req.get("messages") or []
    tools = req.get("tools")
    temperature = req.get("temperature")
    model_name = req.get("model_name")
    base_url = req.get("base_url")
    conversation_id = req.get("conversation_id")
    # the ledger row for this call names the box that spent the key
    box_tok = call_box_id.set(box.id if box is not None else boxes.op_box(op_id))
    # an incognito turn's calls record usage only, never context: the turn's
    # envelope says so; this handler runs outside the turn's own context
    env = broker.get_turn(op_id)
    eph_tok = runtime.ephemeral.set(bool(env is not None and env.ephemeral))
    try:
        async for ev in model.complete(messages, tools=tools,
                                        conversation_id=conversation_id,
                                        temperature=temperature, op_id=op_id,
                                        model_name=model_name, base_url=base_url):
            await _send(loop, conn, ev)
    except (BudgetExceeded, ModelError) as e:
        if isinstance(e, BudgetExceeded):
            await _refused(req, box, "model_call", "budget_exceeded")
        await _send(loop, conn, {"type": "error",
                                 "error": type(e).__name__, "message": str(e)})
    except ConnectionError:
        raise                # the guest stopped reading: nothing to tell it
    except Exception as e:  # noqa: BLE001 — one bad call must not kill the server
        # str() of a timeout or a dropped stream is often "", which reached the
        # operator as "ModelError: " with no clue (2026-09-27)
        await _send(loop, conn, {"type": "error",
                                 "error": type(e).__name__, "message": str(e) or repr(e)})
    finally:
        call_box_id.reset(box_tok)
        runtime.ephemeral.reset(eph_tok)


async def _handle_tool_broker_call(loop, conn, req: dict, box=None,
                                   pending: bytearray | None = None) -> bool:
    op_id = req.get("op_id") or "vm-anon"
    entitled = _entitled(req)
    if (not entitled or broker.get_turn(op_id) is None   # same pinning as model_call
            or _op_from_elsewhere(req, box)):
        await _refused(req, box, "tool_broker_call",
                       "wrong_box" if entitled and broker.get_turn(op_id) is not None
                       else "unknown_op_id")
        await _send(loop, conn, {"type": "error", "error": "unknown_op_id",
                                 "message": f"op_id {op_id!r} is not this caller's turn"})
        return True
    # a task the turn's stop can cancel: spawn_agent, deploy_agents and research
    # run to completion in here, and used to outlive the turn that asked
    res = await _tracked(
        loop, conn, op_id,
        broker.broker_dispatch(op_id, req.get("name") or "", req.get("args") or {},
                               call_id=req.get("call_id")),
        pending if pending is not None else bytearray())
    if res is None:
        return False
    out = {"type": "broker_result", "result": res["result"], "taint": res["taint"]}
    if res.get("image"):
        out["image"] = res["image"]
    await _send(loop, conn, out)
    return False


async def _handle_taint_note(loop, conn, req: dict, box=None) -> bool:
    """A guest-side tool (the WP5 screenshot tool) read untrusted content with
    no broker hop, so the host could not see it: taint the turn as web_read
    would. Only ever ADDS taint, and only for the caller's own turn."""
    op_id = req.get("op_id") or ""
    env = broker.get_turn(op_id)
    entitled = _entitled(req)
    if not entitled or env is None or _op_from_elsewhere(req, box):
        await _refused(req, box, "taint_note",
                       "wrong_box" if entitled and env is not None else "unknown_op_id")
        await _send(loop, conn, {"type": "error", "error": "unknown_op_id",
                                 "message": f"op_id {op_id!r} is not this caller's turn"})
        return True
    src = req.get("source")
    src = src if isinstance(src, str) and len(src) <= 64 and src.isprintable() else "?"
    newly = op_id not in broker._tainted
    if newly:
        # same order as broker_dispatch: /persist goes read-only BEFORE the
        # guest is told the turn is tainted
        from . import persist
        broker.mark_tainted(op_id)
        await persist.on_taint(env.active_project)
    print(f"[gateway] taint_note op={op_id} source={src} newly={newly}")
    await _send(loop, conn, {"type": "taint_noted", "tainted": True, "newly": newly})
    return False


def _package_for(box) -> bytes | None:
    """The guest package for the CALLER's kind, with its box.json. None =
    this kind has no package (yet)."""
    import io
    import json as _json
    import tarfile
    from .guest_pkg import build_package_tar
    if box is None:
        return build_package_tar()           # flag off: today's package, unchanged
    fn = _PACKAGE_BUILDERS.get(box.kind)
    if fn is None and box.kind not in ("shared", "project"):
        return None
    raw = fn(box) if fn is not None else build_package_tar()
    # append box.json: re-pack (a gz stream cannot be appended in place)
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as src, \
            tarfile.open(fileobj=out, mode="w:gz") as dst:
        for m in src.getmembers():
            if m.name == "box.json":
                continue                      # only the host's own identity
            dst.addfile(m, src.extractfile(m) if m.isfile() else None)
        data = _json.dumps(box.box_json(), indent=1).encode()
        ti = tarfile.TarInfo("box.json")
        ti.size, ti.mode = len(data), 0o644
        dst.addfile(ti, io.BytesIO(data))
    return out.getvalue()


def _caller(peer_cid, box):
    """(gated, box): whether kind gating applies, and the caller's box."""
    if not settings.vm_boxes_enabled:
        return False, None
    return True, (box if box is not None else boxes.by_cid(peer_cid))


async def _dispatch(loop, conn, req, *, gated, caller, peer_cid, key,
                    pending: bytearray) -> bool:
    """Answer one request. True when it was refused (counted per connection)."""
    if not isinstance(req, dict):
        await _send(loop, conn, {"type": "error", "error": "bad_json"})
        return True
    op = req.get("op")
    if gated and op != "ping" and (caller is None or not caller.may(op)):
        who = caller.kind if caller is not None else f"unknown (cid {peer_cid})"
        await _refused(req, caller, op, "op_not_allowed")
        await _send(loop, conn, {"type": "error", "error": "op_not_allowed",
                                 "message": f"{op!r} is not allowed from a {who} box"})
        return True
    if op == "model_call":
        return await _handle_model_call(loop, conn, req, caller, pending)
    if op == "tool_broker_call":
        return await _handle_tool_broker_call(loop, conn, req, caller, pending)
    if op == "taint_note":
        return await _handle_taint_note(loop, conn, req, caller)
    if op == "get_guest_package":
        import base64
        if not _rate_ok(key, "package", PACKAGE_PER_MIN):
            raise _Refuse("rate_limited", f"more than {PACKAGE_PER_MIN} package requests a minute")
        pkg = await loop.run_in_executor(None, _package_for, caller)
        if pkg is None:
            await _refused(req, caller, op, "no_package")
            await _send(loop, conn, {"type": "error", "error": "no_package",
                                     "message": f"no package for {caller.kind} boxes"})
            return True
        tar = base64.b64encode(pkg).decode()
        await _send(loop, conn, {"type": "guest_package", "tar_b64": tar})
        return False
    if op in _OP_HANDLERS and gated:
        await _OP_HANDLERS[op](loop, conn, req, caller)
        return False
    if op == "ping":
        await _send(loop, conn, {"type": "pong"})
        return False
    await _refused(req, caller, op, "unknown_op")
    await _send(loop, conn, {"type": "error", "error": "unknown_op",
                             "message": f"op={op!r}"})
    return True


async def handle_conn(loop, conn, *, peer_cid=None, box=None) -> None:
    """Serve one guest connection: read NDJSON requests, dispatch each. Exposed
    (not underscored) so tests can drive it over an AF_UNIX socketpair.

    `peer_cid` is the vsock peer's CID from accept(); `box` is set instead by
    a per-box unix listener (docker). With boxes enabled the caller's kind
    decides which ops it may use (boxes.GATEWAY_OPS); an unknown caller may
    only ping. With boxes off, nothing is gated: today's behaviour.

    The connection is charged against the box's caps (see MAX_* above): a cap
    that trips ends the connection with an error naming it."""
    gated, caller = _caller(peer_cid, box)
    who = caller if caller is not None else box
    key = (who.id if who is not None
           else f"cid:{peer_cid}" if peer_cid is not None else "local")
    claim = _Claim(key)
    buf = bytearray()
    refused = 0
    try:
        claim.acquire()
        while True:
            claim.hold(len(buf))            # the request just served is done with
            line = await _read_line(loop, conn, buf, claim)
            if line is None:
                return
            if not line.strip():
                continue
            claim.hold(len(buf) + len(line))
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                await _send(loop, conn, {"type": "error", "error": "bad_json"})
                refused += 1
            else:
                if await _dispatch(loop, conn, req, gated=gated, caller=caller,
                                   peer_cid=peer_cid, key=key, pending=buf):
                    refused += 1
            if refused > MAX_REFUSALS_PER_CONN:
                raise _Refuse("too_many_refusals",
                              f"more than {MAX_REFUSALS_PER_CONN} refused requests on one connection")
    except _Refuse as r:
        try:
            await _send(loop, conn, {"type": "error", "error": r.cap, "message": r.message})
        except (ConnectionError, OSError):
            pass
        await gateway_log.record_cap_trip(
            r.cap, r.message, who.id if who is not None else key,
            getattr(who, "project", None))
    except (ConnectionError, OSError):
        pass
    finally:
        claim.close()
        try:
            conn.close()
        except OSError:
            pass


class VsockGateway:
    """The AF_VSOCK listener. One per app, started in the FastAPI lifespan. If the
    host lacks vsock (a dev laptop, CI), start() degrades to a no-op so the app
    still runs — the VM path is simply unavailable there."""

    def __init__(self, port: int | None = None):
        self.port = port or settings.vm_vsock_port
        self.enabled = False
        self.connections = 0            # guests seen — the lifecycle readiness signal
        self._sock: socket.socket | None = None
        self._task: asyncio.Task | None = None
        self._unix: dict[str, asyncio.Task] = {}
        self._conns: set[asyncio.Task] = set()      # held: a task nobody references can be collected

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                conn, peer = await loop.sock_accept(self._sock)
            except asyncio.CancelledError:
                raise
            except OSError:
                # a transient accept failure must not end the listener for the
                # app's whole life — that would silently disable the guest
                await asyncio.sleep(0.5)
                continue
            conn.setblocking(False)
            self.connections += 1
            # the peer's CID is the caller's identity (the guest cannot choose
            # it: KVM assigns it from the -device vhost-vsock-pci guest-cid)
            cid = peer[0] if isinstance(peer, tuple) and peer else None
            t = asyncio.create_task(handle_conn(loop, conn, peer_cid=cid))
            self._conns.add(t)
            t.add_done_callback(self._conns.discard)

    async def start(self) -> None:
        try:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            s.bind((socket.VMADDR_CID_ANY, self.port))
            s.listen(128)
            s.setblocking(False)
        except (OSError, AttributeError) as e:
            # no vsock here (laptop/CI) — leave the gateway disabled, app runs on
            print(f"[vm] vsock gateway disabled: {e}")
            return
        self._sock = s
        self.enabled = True
        self._task = asyncio.create_task(self._serve())

    # --- per-box AF_UNIX listeners (docker boxes; docs/boxes-contract.md C) --
    async def listen_unix(self, box) -> None:
        """Listen on <vm_dir>/sock/<cid>/gateway.sock for ONE box. The listener is
        the identity: every connection on it is that box, whatever it says."""
        import os
        path = box.transport.gateway_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(path))
        os.chmod(path, 0o660)
        s.listen(128)
        s.setblocking(False)
        loop = asyncio.get_running_loop()

        async def serve():
            try:
                while True:
                    try:
                        conn, _ = await loop.sock_accept(s)
                    except OSError:
                        await asyncio.sleep(0.5)
                        continue
                    conn.setblocking(False)
                    t = asyncio.create_task(handle_conn(loop, conn, box=box))
                    self._conns.add(t)
                    t.add_done_callback(self._conns.discard)
            finally:
                s.close()
        await self.unlisten_unix(box.id)
        self._unix[box.id] = asyncio.create_task(serve())

    async def unlisten_unix(self, box_id: str) -> None:
        t = self._unix.pop(box_id, None)
        if t is not None:
            t.cancel()

    async def stop(self) -> None:
        for bid in list(self._unix):
            await self.unlisten_unix(bid)
        if self._task:
            self._task.cancel()
        for t in list(self._conns):
            t.cancel()
        if self._sock:
            self._sock.close()
        self.enabled = False


# module-level singleton, started/stopped by the app lifespan
gateway = VsockGateway()
