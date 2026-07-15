"""Guest-side `registry` shim. loop.py imports `registry` and calls
openai_tool_specs()/read_only_names()/dispatch(). The host pushes the tool-spec
snapshot in the turn spec (the guest never compiles it). dispatch() sends a
`tool_broker_call` over vsock to the host, which runs the real tool behind every
gate and returns the result — M2 brokers ALL tools; M3 will run the clean
file/search tools locally against a pushed workspace and broker only the rest."""
import asyncio
import json
import socket

HOST_CID = socket.VMADDR_CID_HOST          # 2

_specs: list[dict] = []
_read_only: frozenset[str] = frozenset()
_op_id: str | None = None
_gateway_port = 5555


def set_registry(specs, read_only) -> None:
    global _specs, _read_only
    _specs = specs or []
    _read_only = frozenset(read_only or [])


def set_turn(op_id, gateway_port=None) -> None:
    global _op_id, _gateway_port
    _op_id = op_id
    if gateway_port:
        _gateway_port = gateway_port


def openai_tool_specs(entries=None) -> list[dict]:
    return _specs


def read_only_names(entries=None) -> frozenset[str]:
    return _read_only


async def dispatch(name: str, args: dict) -> str:
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    await loop.run_in_executor(None, s.connect, (HOST_CID, _gateway_port))
    s.setblocking(False)
    try:
        req = {"op": "tool_broker_call", "op_id": _op_id, "name": name, "args": args}
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
