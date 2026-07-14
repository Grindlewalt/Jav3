"""Guest-side `model` shim. loop.py imports `model` and calls
`model.complete(...)`; in the guest that dials the host gateway over vsock
(guest -> host, CID 2) and relays its streamed events. The DeepSeek key, peak
gate, budget metering, and DSML recovery all stay host-side in the gateway — this
is a thin relay that attaches the turn's op_id and re-raises a budget/model stop.
"""
import asyncio
import json
import socket

from .budget import BudgetExceeded

HOST_CID = socket.VMADDR_CID_HOST          # 2 — the host, from inside the guest


class ModelError(Exception):
    pass


class VsockModelClient:
    def __init__(self):
        self.op_id: str | None = None
        self.gateway_port = 5555            # settings.vm_vsock_port; set per turn

    def set_turn(self, op_id: str | None, gateway_port: int | None = None) -> None:
        self.op_id = op_id
        if gateway_port:
            self.gateway_port = gateway_port

    async def complete(self, messages, tools=None, conversation_id=None,
                       temperature=None, model_name=None, base_url=None):
        loop = asyncio.get_running_loop()
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        s.setblocking(False)
        await loop.sock_connect(s, (HOST_CID, self.gateway_port))
        req = {"op": "model_call", "op_id": self.op_id, "messages": messages,
               "tools": tools, "temperature": temperature,
               "conversation_id": conversation_id, "model_name": model_name,
               "base_url": base_url}
        try:
            await loop.sock_sendall(s, (json.dumps(req) + "\n").encode())
            buf = b""
            while True:
                while b"\n" not in buf:
                    chunk = await loop.sock_recv(s, 65536)
                    if not chunk:
                        return
                    buf += chunk
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                ev = json.loads(line)
                kind = ev.get("type")
                if kind == "token":
                    yield ev
                elif kind == "message":
                    yield ev
                    return
                elif kind == "error":
                    if ev.get("error") == "BudgetExceeded":
                        raise BudgetExceeded(ev.get("message", ""))
                    raise ModelError(ev.get("message", ""))
        finally:
            s.close()


model = VsockModelClient()
