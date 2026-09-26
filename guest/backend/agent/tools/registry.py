"""Guest-side `registry` shim. loop.py imports `registry` and calls
openai_tool_specs()/read_only_names()/dispatch(). The host pushes the tool-spec
snapshot in the turn spec (the guest never compiles it).

dispatch() forks: the CLEAN file/search tools run IN the guest — their handler is
loaded from the pushed tools/ dir and executed against the pushed workspace copy;
everything else (web, secrets, memory, git, spawn/deploy) sends a tool_broker_call
over vsock so the host runs it behind every gate."""
import asyncio
import contextvars
import importlib.util
import inspect
import json
import socket
import traceback

from ... import turnctx
from ...config import settings
from .. import imageresult

HOST_CID = socket.VMADDR_CID_HOST          # 2

# tools safe to run in the disposable guest against the pushed workspace
IN_GUEST_TOOLS = frozenset({"read_file", "list_files", "search_codebase",
                            "crawl_codebase", "write_file", "edit_file",
                            "dashboard", "todo_update", "run_code"})

# the model's id for the call being dispatched (set by loop.py around
# dispatch). Forwarded on tool_broker_call so a host handler can name the call
# the way the chat stream does — correlation only, the host trusts nothing by it.
call_id = contextvars.ContextVar("jav3_registry_call_id", default=None)

# handler modules are stateless and keyed by name, so this cache is safely shared
# across turns; the per-turn state (specs, op_id, ...) lives in turnctx.
_handlers: dict[str, object] = {}


def set_registry(specs, read_only) -> None:
    """Test/direct-use setter: bind the tool snapshot into the turn context.
    The run-turn server uses turnctx.enter() instead."""
    turnctx.specs.set(tuple(specs or ()))
    turnctx.read_only.set(frozenset(read_only or []))


def set_turn(op_id, gateway_port=None, op_token=None) -> None:
    turnctx.op_id.set(op_id)
    # the op_id alone is not a credential — the gateway rejects a call that
    # cannot also present the turn's token (broker._op_tokens)
    turnctx.op_token.set(op_token)
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
               "op_token": turnctx.op_token.get(), "name": name, "args": args,
               "call_id": call_id.get()}
        await loop.sock_sendall(s, (json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = await loop.sock_recv(s, 65536)
            if not chunk:
                return "error: broker connection closed"
            buf += chunk
        ev = json.loads(buf.split(b"\n", 1)[0])
        if ev.get("type") == "broker_result":
            result = ev.get("result", "")
            img = ev.get("image")
            # a host tool's image rides the reply inline (a host path is
            # meaningless here); hand it to the loop the way an in-guest tool
            # would. The loop sniffs and caps it before the model sees it.
            if isinstance(img, dict) and isinstance(img.get("b64"), str):
                cap = img.get("caption")
                return imageresult.with_inline(
                    result, b64=img["b64"], mime=img.get("mime"),
                    caption=cap if isinstance(cap, str) else None)
            return result
        if ev.get("type") == "error":
            return f"error: broker {ev.get('error')}: {ev.get('message', '')}"
        return f"error: unexpected broker reply {ev.get('type')!r}"
    finally:
        s.close()
