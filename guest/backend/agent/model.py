"""Guest-side `model` shim. loop.py imports `model` and calls
`model.complete(...)`; in the guest that dials the host gateway over vsock
(guest -> host, CID 2) and relays its streamed events. The DeepSeek key,
budget metering, and DSML recovery all stay host-side in the gateway — this
is a thin relay that attaches the turn's op_id and re-raises a budget/model stop.

The op_id and gateway port are read from `turnctx` (task-local), not instance
state, so concurrent turns sharing this one client never cross their op_ids.
"""
import asyncio
import json
import socket

from .. import boxinfo, turnctx
from .budget import BudgetExceeded

HOST_CID = socket.VMADDR_CID_HOST          # 2 — the host, from inside the guest


class ModelError(Exception):
    pass


class VsockModelClient:
    async def complete(self, messages, tools=None, conversation_id=None,
                       temperature=None, model_name=None, base_url=None):
        loop = asyncio.get_running_loop()
        if boxinfo.unix_gateway():      # a docker box: per-box unix socket
            s = await loop.run_in_executor(None, boxinfo.gateway_connect)
        else:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            # blocking connect in an executor (works under any event loop, incl.
            # uvloop whose sock_connect getaddrinfo-chokes on a vsock (cid,port))
            await loop.run_in_executor(None, s.connect,
                                       (HOST_CID, turnctx.gateway_port.get()))
        s.setblocking(False)
        req = {"op": "model_call", "op_id": turnctx.op_id.get(),
               "op_token": turnctx.op_token.get(), "messages": messages,
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
                        raise ModelError("the model gateway closed the connection "
                                         "before the reply was complete")
                    buf += chunk
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                ev = json.loads(line)
                kind = ev.get("type")
                if kind in ("token", "retry"):     # retry: the stream dropped, text so far is void
                    yield ev
                elif kind == "message":
                    yield ev
                    return
                elif kind == "error":
                    if ev.get("error") == "BudgetExceeded":
                        raise BudgetExceeded(ev.get("message", ""))
                    raise ModelError(f"{ev.get('error') or 'error'}: "
                                     f"{ev.get('message') or '(no detail)'}")
        finally:
            s.close()


model = VsockModelClient()
