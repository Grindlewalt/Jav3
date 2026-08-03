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

# One guest, one workspace dir per slug. Unpacking rmtree's that dir, so only
# the FIRST concurrent operation on a slug may ship a fresh copy — later ones
# join the existing one (like nested turns always have), and the LAST one out
# sweeps the shared write buffer home. Single event loop: plain dict, but the
# check+increment must happen with no await in between.
_ws_holds: dict[str, int] = {}


def acquire_workspace(slug: str) -> bool:
    """Register a workspace user; True when this caller should push the copy."""
    n = _ws_holds.get(slug, 0)
    _ws_holds[slug] = n + 1
    return n == 0


def release_workspace(slug: str) -> bool:
    """Drop a hold; True when this caller was the last one out."""
    n = _ws_holds.get(slug, 1) - 1
    if n <= 0:
        _ws_holds.pop(slug, None)
        return True
    _ws_holds[slug] = n
    return False

_CONFIG_KNOBS = (
    "max_react_iterations", "subagent_max_iterations", "dead_end_force_answer",
    "dead_end_error_streak", "delegate_nudge_round", "tool_result_max_chars",
    "tool_result_keep_recent", "tool_result_evict_chars",
    "plan_recheck_every", "web_handroll_nudge",
)


def config_snapshot() -> dict:
    return {k: getattr(settings, k) for k in _CONFIG_KNOBS}


async def guest_turn(conversation_id, system_prompt, history, *, rules="",
                     tool_specs=None, read_only=None, op_id=None, envelope=None,
                     active_slug=None, push_workspace=False, model_name=None,
                     base_url=None, self_check=True, max_iterations=None,
                     rewrite_rules=True):
    """Run one turn in the guest, yielding its events. Raises on a transport
    failure (connect/read) so the caller can fall back or surface an error.

    `envelope` (a broker.TurnEnvelope) is registered host-side by op_id for the
    turn's tool_broker_calls; the guest never carries it.

    `active_slug` is the project the guest's in-guest file tools operate on.
    `push_workspace` asks for that project's workspace in the guest, with its
    write buffer coming home at turn end — set for a TOP-LEVEL turn. A NESTED
    turn (spawn_agent/deploy_agents child) leaves it False: it reuses the copy its
    parent already pushed into the same guest and its edits ride home on the
    parent's turn-end pack. Among CONCURRENT top-level turns on one slug, only
    the first actually ships a copy (see _ws_holds above); the last one out
    sweeps the shared buffer.

    A nested turn passes an op_id already carrying the operation's Budget; a
    top-level turn's op_id is fresh, and it inherits the operation's Budget if one
    is in scope (contextvar) so every turn in one operation meters into one Budget."""
    op_id = op_id or f"guest:{conversation_id}"
    owns_budget = budget_mod.get(op_id) is None
    if owns_budget:
        # share the operation's Budget object if we're inside one (nested), else
        # open this operation's own — release() later drops only this id's alias.
        inherited = budget_mod.current()
        budget_mod.register(op_id, inherited or Budget(
            settings.max_op_input_tokens, settings.max_op_output_tokens))
    if envelope is not None:
        broker.register_turn(envelope)
    holds_ws = bool(push_workspace and active_slug)
    # first-in pushes a fresh copy; joiners reuse it (no await between check+set)
    owns_ws = acquire_workspace(active_slug) if holds_ws else False
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
        # voice turns skip the second-pass rules rewrite — the streamed text was
        # already spoken aloud, so rewriting it costs a model call and changes
        # nothing the operator will hear
        "rewrite_rules": rewrite_rules,
        "max_iterations": max_iterations,
        "config": config_snapshot(),
        "active_slug": active_slug,
    }
    if owns_ws:
        # ship the workspace so the in-guest file tools work on a copy; the
        # guest's write buffer comes back after the turn.
        spec["workspace_tar_b64"] = base64.b64encode(
            workspace_xfer.build_merged_tar(active_slug)).decode()
    from .lifecycle import vm as guest_vm
    await guest_vm.acquire()          # boot + pin the guest for this turn's life
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
                # the guest's write buffer, sent AFTER `final` — apply it
                # host-side (writes.apply_write: secret refusal + advisory diff
                # gate) and don't surface it to the caller. Only the workspace
                # owner receives this; joiner/nested edits ride the shared
                # buffer. The stream ends when the guest closes.
                if owns_ws:
                    await workspace_xfer.apply_guest_writes(
                        active_slug, base64.b64decode(ev.get("tar_b64") or ""))
                continue
            yield ev
    finally:
        s.close()
        if holds_ws and release_workspace(active_slug) and not owns_ws:
            # last one out of a shared workspace, and the owner's turn-end pack
            # already happened (or never will): sweep the buffer home. Repeat
            # applies of the same bytes are idempotent.
            try:
                await pull_writes(active_slug)
            except Exception:  # noqa: BLE001 — best-effort sweep
                pass
        guest_vm.release()
        if envelope is not None:
            broker.release_turn(op_id)
        if owns_budget:
            budget_mod.release(op_id)


async def _guest_rpc(spec: dict) -> dict | None:
    """One short request/response to the guest run-turn server (prime / pull)."""
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    try:
        await loop.run_in_executor(
            None, s.connect, (settings.vm_guest_cid, GUEST_RUNTURN_PORT))
        s.setblocking(False)
        await loop.sock_sendall(s, (json.dumps(spec) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = await loop.sock_recv(s, 65536)
            if not chunk:
                return None
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])
    finally:
        s.close()


async def prime_workspace(slug: str) -> None:
    """Push ONE fresh workspace copy for an operation whose turns will reuse it
    (orchestrator leaves fan out concurrently on one project — priming once up
    front avoids each leaf racing a fresh unpack of the shared guest dir).
    Callers hold the slug via acquire_workspace and prime only when first in."""
    tar_b64 = base64.b64encode(workspace_xfer.build_merged_tar(slug)).decode()
    await _guest_rpc({"mode": "prime", "active_slug": slug,
                      "workspace_tar_b64": tar_b64})


async def pull_writes(slug: str) -> None:
    """Pull the operation's accumulated guest write buffer and apply it host-side
    (secret refusal + advisory diff gate) — the counterpart to prime_workspace."""
    ev = await _guest_rpc({"mode": "pull", "active_slug": slug})
    if ev and ev.get("type") == "staged":
        await workspace_xfer.apply_guest_writes(
            slug, base64.b64decode(ev.get("tar_b64") or ""))
