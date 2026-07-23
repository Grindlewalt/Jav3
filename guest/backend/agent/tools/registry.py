"""Guest-side `registry` shim. loop.py imports `registry` and calls
openai_tool_specs()/read_only_names()/dispatch(). The host pushes the tool-spec
snapshot in the turn spec (the guest never compiles it).

dispatch() forks: the CLEAN file/search tools run IN the guest — their handler is
loaded from the pushed tools/ dir and executed against the pushed workspace copy;
everything else (web, secrets, memory, git, spawn/deploy) sends a tool_broker_call
over vsock so the host runs it behind every gate."""
import asyncio
import importlib.util
import inspect
import json
import socket
import traceback

from ... import turnctx
from ...config import settings

HOST_CID = socket.VMADDR_CID_HOST          # 2

# tools safe to run in the disposable guest against the pushed workspace
IN_GUEST_TOOLS = frozenset({"read_file", "list_files", "search_codebase",
                            "crawl_codebase", "write_file", "edit_file",
                            "dashboard", "todo_update", "run_code"})

# handler modules are stateless and keyed by name, so this cache is safely shared
# across turns; the per-turn state (specs, op_id, ...) lives in turnctx.
_handlers: dict[str, object] = {}


def set_registry(specs, read_only) -> None:
    """Test/direct-use setter: bind the tool snapshot into the turn context.
    The run-turn server uses turnctx.enter() instead."""
    turnctx.specs.set(tuple(specs or ()))
    turnctx.read_only.set(frozenset(read_only or []))


def set_turn(op_id, gateway_port=None) -> None:
    turnctx.op_id.set(op_id)
    if gateway_port:
        turnctx.gateway_port.set(gateway_port)


def openai_tool_specs(entries=None) -> list[dict]:
    return list(turnctx.specs.get())


def read_only_names(entries=None) -> frozenset[str]:
    return turnctx.read_only.get()


async def dispatch(name: str, args: dict) -> str:
    if name in IN_GUEST_TOOLS:
        return await _local_dispatch(name, args)
    return await _broker_dispatch(name, args)


def _load_handler(name: str):
    cached = _handlers.get(name)
    if cached is not None:
        return cached
    path = settings.tools_dir / name / "handler.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"guest_tool_{name}", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, "run", None)
    if fn is not None:
        _handlers[name] = fn
    return fn


async def _local_dispatch(name: str, args: dict) -> str:
    try:
        handler = _load_handler(name)
    except Exception as e:  # noqa: BLE001 — a broken pushed handler must not kill the turn
        return (f"error: in-guest tool '{name}' handler failed to load: "
                f"{type(e).__name__}: {e}. Use a different tool.")
    if handler is None:
        return f"error: in-guest tool '{name}' has no handler in the pushed package"
    try:
        # bind first so only argument mismatches read as "bad arguments"
        inspect.signature(handler).bind(**args)
    except TypeError as e:
        return (f"error: bad arguments for '{name}': {e}. Check the schema and "
                "retry with corrected arguments.")
    try:
        return await handler(**args)
    except Exception as e:  # noqa: BLE001 — the loop must observe failures, not die
        return (f"error: {name} failed with {type(e).__name__}: {e}. Adjust the "
                f"arguments or try a different approach.\n{traceback.format_exc(limit=4)}")


async def _broker_dispatch(name: str, args: dict) -> str:
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    await loop.run_in_executor(None, s.connect, (HOST_CID, turnctx.gateway_port.get()))
    s.setblocking(False)
    try:
        req = {"op": "tool_broker_call", "op_id": turnctx.op_id.get(),
               "name": name, "args": args}
        await loop.sock_sendall(s, (json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = await loop.sock_recv(s, 65536)
            if not chunk:
                return "error: broker connection closed"
            buf += chunk
        ev = json.loads(buf.split(b"\n", 1)[0])
        if ev.get("type") == "broker_result":
            return ev.get("result", "")
        if ev.get("type") == "error":
            return f"error: broker {ev.get('error')}: {ev.get('message', '')}"
        return f"error: unexpected broker reply {ev.get('type')!r}"
    finally:
        s.close()
