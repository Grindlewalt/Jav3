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
from ..agent.model import ModelError, PeakPricingConfirmationRequired, model
from ..config import settings
from . import broker


async def _send(loop, conn, obj: dict) -> None:
    await loop.sock_sendall(conn, (json.dumps(obj) + "\n").encode())


async def _handle_model_call(loop, conn, req: dict) -> None:
    op_id = req.get("op_id") or "vm-anon"
    # op_id pinning: only a turn the host already registered (guest_turn) may
    # spend. The gateway never opens a budget itself — a compromised guest can't
    # invent op_ids to escape the per-operation cap by rotating ids.
    if budget_mod.get(op_id) is None:
        await _send(loop, conn, {"type": "error", "error": "unknown_op_id",
                                 "message": f"op_id {op_id!r} is not a registered turn"})
        return
    messages = req.get("messages") or []
    tools = req.get("tools")
    temperature = req.get("temperature")
    model_name = req.get("model_name")
    base_url = req.get("base_url")
    conversation_id = req.get("conversation_id")
    try:
        async for ev in model.complete(messages, tools=tools,
                                        conversation_id=conversation_id,
                                        temperature=temperature, op_id=op_id,
                                        model_name=model_name, base_url=base_url):
            await _send(loop, conn, ev)
    except (PeakPricingConfirmationRequired, BudgetExceeded, ModelError) as e:
        await _send(loop, conn, {"type": "error",
                                 "error": type(e).__name__, "message": str(e)})
    except Exception as e:  # noqa: BLE001 — one bad call must not kill the server
        await _send(loop, conn, {"type": "error",
                                 "error": type(e).__name__, "message": str(e)})


async def _handle_tool_broker_call(loop, conn, req: dict) -> None:
    op_id = req.get("op_id") or "vm-anon"
    if broker.get_turn(op_id) is None:      # same pinning as model_call
        await _send(loop, conn, {"type": "error", "error": "unknown_op_id",
                                 "message": f"op_id {op_id!r} has no broker turn context"})
        return
    res = await broker.broker_dispatch(op_id, req.get("name") or "", req.get("args") or {})
    await _send(loop, conn, {"type": "broker_result",
                             "result": res["result"], "taint": res["taint"]})


async def handle_conn(loop, conn) -> None:
    """Serve one guest connection: read NDJSON requests, dispatch each. Exposed
    (not underscored) so tests can drive it over an AF_UNIX socketpair."""
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
            if op == "model_call":
                await _handle_model_call(loop, conn, req)
            elif op == "tool_broker_call":
                await _handle_tool_broker_call(loop, conn, req)
            elif op == "get_guest_package":
                import base64
                from .guest_pkg import build_package_tar
                tar = base64.b64encode(build_package_tar()).decode()
                await _send(loop, conn, {"type": "guest_package", "tar_b64": tar})
            elif op == "ping":
                await _send(loop, conn, {"type": "pong"})
            else:
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

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                conn, _ = await loop.sock_accept(self._sock)
            except asyncio.CancelledError:
                raise
            except OSError:
                # a transient accept failure must not end the listener for the
                # app's whole life — that would silently disable the guest
                await asyncio.sleep(0.5)
                continue
            conn.setblocking(False)
            self.connections += 1
            asyncio.create_task(handle_conn(loop, conn))

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

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._sock:
            self._sock.close()
        self.enabled = False


# module-level singleton, started/stopped by the app lifespan
gateway = VsockGateway()
