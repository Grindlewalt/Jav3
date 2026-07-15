"""The guest run-turn server. Listens on the guest's vsock CID; the host
`guest_turn` connects and sends one `run_turn` spec (newline-delimited JSON);
this runs the real ReAct loop in the guest and streams its four event types
(token / tool / tool_result / final) straight back. Model calls the loop makes
dial back out to the host gateway (see agent/model.py). Nothing durable lives
here — the guest holds no key, no DB, no memory.

Run as: python3 -m jarvis_guest.server
"""
import asyncio
import json
import socket

from . import config as guest_config
from . import memory as guest_memory
from .agent import model as guest_model
from .agent.loop import run_turn
from .agent.tools import registry as guest_registry

PORT = 5556                                 # guest run-turn server (host dials this)


async def _handle(loop, conn) -> None:
    try:
        buf = b""
        while b"\n" not in buf:
            chunk = await loop.sock_recv(conn, 65536)
            if not chunk:
                return
            buf += chunk
        line, _ = buf.split(b"\n", 1)
        spec = json.loads(line)

        # apply the pushed turn context to the shims the loop reads
        guest_config.apply(spec.get("config"))
        guest_memory.set_rules(spec.get("rules", ""))
        guest_registry.set_registry(spec.get("tool_specs"), spec.get("read_only"))
        guest_registry.set_turn(spec.get("op_id"), spec.get("gateway_port"))
        guest_model.model.set_turn(spec.get("op_id"), spec.get("gateway_port"))

        async def send(ev: dict) -> None:
            await loop.sock_sendall(conn, (json.dumps(ev) + "\n").encode())

        try:
            async for ev in run_turn(
                    spec.get("conversation_id") or 0,
                    spec["system_prompt"],
                    spec.get("history") or [],
                    tools=spec.get("tool_specs"),
                    model_name=spec.get("model_name"),
                    base_url=spec.get("base_url"),
                    self_check=spec.get("self_check", True),
                    max_iterations=spec.get("max_iterations"),
                    on_tool_call=None):
                await send(ev)
        except Exception as e:  # noqa: BLE001 — surface any loop crash as a final
            await send({"type": "final",
                        "content": f"(guest loop error: {type(e).__name__}: {e})"})
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


async def serve() -> None:
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    s.bind((socket.VMADDR_CID_ANY, PORT))
    s.listen(4)
    s.setblocking(False)
    print(f"GUEST-RUNTURN-SERVER: listening on vsock :{PORT}", flush=True)
    while True:
        conn, _ = await loop.sock_accept(s)
        conn.setblocking(False)
        asyncio.create_task(_handle(loop, conn))


if __name__ == "__main__":
    asyncio.run(serve())
