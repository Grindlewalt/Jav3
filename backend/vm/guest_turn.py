"""Host-side driver for a turn that runs INSIDE the guest.

Same event contract as `run_turn` (yields token / tool / tool_result / final), so
a caller swaps `run_turn(...)` for `guest_turn(...)`. It resolves the rules +
config host-side, registers the op_id budget, connects to the guest's run-turn
server over vsock (host -> guest, the guest's CID), ships the turn spec, and
re-yields the guest's streamed events. The loop's own model calls dial back to
the host gateway (guest -> host); the op_id ties both to one host-side budget.

M1 runs no-tools turns; M2 adds tool_specs + host tool-brokering + tool-call
persistence reconstructed here from the tool/tool_result events.
"""
import asyncio
import base64
import json
import socket

from ..agent import budget as budget_mod
from ..agent.budget import Budget
from ..config import settings
from . import broker, workspace_xfer

GUEST_RUNTURN_PORT = 5556                   # must match jarvis_guest.server.PORT

_CONFIG_KNOBS = (
    "max_react_iterations", "subagent_max_iterations", "dead_end_force_answer",
    "dead_end_error_streak", "delegate_nudge_round", "tool_result_max_chars",
    "tool_result_keep_recent", "tool_result_evict_chars",
)


def config_snapshot() -> dict:
    return {k: getattr(settings, k) for k in _CONFIG_KNOBS}


async def guest_turn(conversation_id, system_prompt, history, *, rules="",
                     tool_specs=None, read_only=None, op_id=None, envelope=None,
                     workspace_slug=None, model_name=None, base_url=None,
                     self_check=True, max_iterations=None):
    """Run one turn in the guest, yielding its events. Raises on a transport
    failure (connect/read) so the caller can fall back or surface an error.

    `envelope` (a broker.TurnEnvelope) is registered host-side by op_id for the
    turn's tool_broker_calls; the guest never carries it."""
    op_id = op_id or f"guest:{conversation_id}"
    owns_budget = budget_mod.get(op_id) is None
    if owns_budget:
        budget_mod.register(op_id, Budget(settings.max_op_input_tokens,
                                          settings.max_op_output_tokens))
    if envelope is not None:
        broker.register_turn(envelope)
    spec = {
        "conversation_id": conversation_id,
        "system_prompt": system_prompt,
        "history": history,
        "rules": rules,
        "tool_specs": tool_specs or [],
        "read_only": list(read_only or []),
        "op_id": op_id,
        "gateway_port": settings.vm_vsock_port,
        "model_name": model_name,
        "base_url": base_url,
        "self_check": self_check,
        "max_iterations": max_iterations,
        "config": config_snapshot(),
    }
    if workspace_slug:
        # push the effective workspace (canonical + Jarvis's staged edits) so the
        # in-guest file tools work on a copy; staged edits come back after the turn.
        spec["workspace_slug"] = workspace_slug
        spec["workspace_tar_b64"] = base64.b64encode(
            workspace_xfer.build_merged_tar(workspace_slug)).decode()
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    try:
        # blocking connect in an executor: uvloop's sock_connect runs getaddrinfo
        # on the address and chokes on an AF_VSOCK (cid, port) tuple. Once
        # connected, sock_sendall/sock_recv work fine under uvloop.
        await loop.run_in_executor(
            None, s.connect, (settings.vm_guest_cid, GUEST_RUNTURN_PORT))
        s.setblocking(False)
        await loop.sock_sendall(s, (json.dumps(spec) + "\n").encode())
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
            if ev.get("type") == "staged":
                # the guest's staged edits, sent AFTER `final` — reconcile them
                # host-side (host stage_write + secret scan) and don't surface it
                # to the caller. The stream ends when the guest closes.
                if workspace_slug:
                    workspace_xfer.reconcile_staged(
                        workspace_slug, base64.b64decode(ev.get("tar_b64") or ""))
                continue
            yield ev
    finally:
        s.close()
        if envelope is not None:
            broker.release_turn(op_id)
        if owns_budget:
            budget_mod.release(op_id)
