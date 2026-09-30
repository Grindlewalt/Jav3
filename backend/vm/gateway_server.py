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
import socket

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


async def _send(loop, conn, obj: dict) -> None:
    await loop.sock_sendall(conn, (json.dumps(obj) + "\n").encode())


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


async def _handle_model_call(loop, conn, req: dict, box=None) -> None:
    op_id = req.get("op_id") or "vm-anon"
    entitled = _entitled(req)
    if not entitled or _op_from_elsewhere(req, box):
        await _refused(req, box, "model_call", "unknown_op_id" if not entitled else "wrong_box")
        await _send(loop, conn, {"type": "error", "error": "unknown_op_id",
                                 "message": f"op_id {op_id!r} is not this caller's turn"})
        return
    # op_id pinning: only a turn the host already registered (guest_turn) may
    # spend. The gateway never opens a budget itself — a compromised guest can't
    # invent op_ids to escape the per-operation cap by rotating ids.
    if budget_mod.get(op_id) is None:
        await _refused(req, box, "model_call", "unknown_op_id")
        await _send(loop, conn, {"type": "error", "error": "unknown_op_id",
                                 "message": f"op_id {op_id!r} is not a registered turn"})
        return
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
    except Exception as e:  # noqa: BLE001 — one bad call must not kill the server
        # str() of a timeout or a dropped stream is often "", which reached the
        # operator as "ModelError: " with no clue (2026-09-27)
        await _send(loop, conn, {"type": "error",
                                 "error": type(e).__name__, "message": str(e) or repr(e)})
    finally:
        call_box_id.reset(box_tok)
        runtime.ephemeral.reset(eph_tok)


async def _handle_tool_broker_call(loop, conn, req: dict, box=None) -> None:
    op_id = req.get("op_id") or "vm-anon"
    entitled = _entitled(req)
    if (not entitled or broker.get_turn(op_id) is None   # same pinning as model_call
            or _op_from_elsewhere(req, box)):
        await _refused(req, box, "tool_broker_call",
                       "wrong_box" if entitled and broker.get_turn(op_id) is not None
                       else "unknown_op_id")
        await _send(loop, conn, {"type": "error", "error": "unknown_op_id",
                                 "message": f"op_id {op_id!r} is not this caller's turn"})
        return
    res = await broker.broker_dispatch(op_id, req.get("name") or "", req.get("args") or {},
                                       call_id=req.get("call_id"))
    out = {"type": "broker_result", "result": res["result"], "taint": res["taint"]}
    if res.get("image"):
        out["image"] = res["image"]
    await _send(loop, conn, out)


async def _handle_taint_note(loop, conn, req: dict, box=None) -> None:
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
        return
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


async def handle_conn(loop, conn, *, peer_cid=None, box=None) -> None:
    """Serve one guest connection: read NDJSON requests, dispatch each. Exposed
    (not underscored) so tests can drive it over an AF_UNIX socketpair.

    `peer_cid` is the vsock peer's CID from accept(); `box` is set instead by
    a per-box unix listener (docker). With boxes enabled the caller's kind
    decides which ops it may use (boxes.GATEWAY_OPS); an unknown caller may
    only ping. With boxes off, nothing is gated: today's behaviour."""
    gated, caller = _caller(peer_cid, box)
    try:
        buf = b""
        while True:
            while b"\n" not in buf:
                chunk = await loop.sock_recv(conn, 65536)
                if not chunk:
                    return
                buf += chunk
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                await _send(loop, conn, {"type": "error", "error": "bad_json"})
                continue
            op = req.get("op")
            if gated and op != "ping" and (caller is None or not caller.may(op)):
                who = caller.kind if caller is not None else f"unknown (cid {peer_cid})"
                await _refused(req, caller, op, "op_not_allowed")
                await _send(loop, conn, {"type": "error", "error": "op_not_allowed",
                                         "message": f"{op!r} is not allowed from a {who} box"})
                continue
            if op == "model_call":
                await _handle_model_call(loop, conn, req, caller)
            elif op == "tool_broker_call":
                await _handle_tool_broker_call(loop, conn, req, caller)
            elif op == "taint_note":
                await _handle_taint_note(loop, conn, req, caller)
            elif op == "get_guest_package":
                import base64
                pkg = _package_for(caller)
                if pkg is None:
                    await _refused(req, caller, op, "no_package")
                    await _send(loop, conn, {"type": "error", "error": "no_package",
                                             "message": f"no package for {caller.kind} boxes"})
                    continue
                tar = base64.b64encode(pkg).decode()
                await _send(loop, conn, {"type": "guest_package", "tar_b64": tar})
            elif op in _OP_HANDLERS and gated:
                await _OP_HANDLERS[op](loop, conn, req, caller)
            elif op == "ping":
                await _send(loop, conn, {"type": "pong"})
            else:
                await _refused(req, caller, op, "unknown_op")
                await _send(loop, conn, {"type": "error", "error": "unknown_op",
                                         "message": f"op={op!r}"})
    except (ConnectionError, OSError):
        pass
    finally:
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
            asyncio.create_task(handle_conn(loop, conn, peer_cid=cid))

    async def start(self) -> None:
        try:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            s.bind((socket.VMADDR_CID_ANY, self.port))
            s.listen(8)
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
        s.listen(8)
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
                    asyncio.create_task(handle_conn(loop, conn, box=box))
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
        if self._sock:
            self._sock.close()
        self.enabled = False


# module-level singleton, started/stopped by the app lifespan
gateway = VsockGateway()
