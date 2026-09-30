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
import logging
import secrets
import socket  # noqa: F401 -- tests patch gt.socket.socket

from ..agent import budget as budget_mod
from ..agent.budget import Budget
from .. import taintpaths
from .. import turnstats
from ..config import settings
from . import boxes, broker, workspace_xfer
from . import persist as persist_mod

log = logging.getLogger("jav3.guest_turn")

GUEST_RUNTURN_PORT = 5556                   # must match jarvis_guest.server.PORT

# One short RPC to a guest (prime / pull), and the wait for the write buffer
# that follows a turn's `final`: a guest that hangs must not wedge a stop or
# keep the VM pinned. The rescue pull in a turn's `finally` gets a shorter
# leash, since the guest may simply be gone.
RPC_TIMEOUT = 120.0
RESCUE_TIMEOUT = 20.0

# The guest is the hostile side: one line it sends (an event, or the write buffer
# as base64) is read into host memory, so it is bounded. The biggest honest line
# is the turn-end buffer; workspace_xfer caps what that may unpack to.
MAX_LINE = 192 * 1024 * 1024


class GuestStreamError(ConnectionError):
    """The guest's stream ended before the turn did: the socket closed with no
    `final` (the guest crashed or ran out of memory, or its VM was reaped)."""


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
    "read_file_max_chars",
    "tool_result_keep_recent", "tool_result_evict_chars", "tool_result_pressure_chars",
    "plan_recheck_every", "web_handroll_nudge",
)


def config_snapshot() -> dict:
    return {k: getattr(settings, k) for k in _CONFIG_KNOBS}


async def guest_turn(conversation_id, system_prompt, history, *, rules="",
                     tool_specs=None, read_only=None, op_id=None, envelope=None,
                     active_slug=None, push_workspace=False, model_name=None,
                     base_url=None, self_check=True, max_iterations=None,
                     rewrite_rules=True, inject_rules=True, inbox=False,
                     persist=False):
    """Run one turn in the guest, yielding its events. Raises on a transport
    failure (connect/read, or a stream that ends without `final`) so the caller
    can fall back or surface an error.

    `envelope` (a broker.TurnEnvelope) is registered host-side by op_id for the
    turn's tool_broker_calls; the guest never carries it.

    `active_slug` is the project the guest's in-guest file tools operate on.
    `push_workspace` asks for that project's workspace in the guest, with its
    write buffer coming back at turn end — set for a TOP-LEVEL turn. A NESTED
    turn (spawn_agent/deploy_agents child) leaves it False: it reuses the copy its
    parent already pushed into the same guest and its edits ride home on the
    parent's turn-end pack. Among CONCURRENT top-level turns on one slug, only
    the first actually ships a copy (see _ws_holds above); the last one out
    sweeps the shared buffer. A turn that stops before its edits came home (an
    operator stop, a dropped stream) pulls them in its `finally`.

    The workspace owner's `final` is held until its edits are applied, so a file
    the host refused or could not write is told in the answer (see
    workspace_xfer.describe_unapplied) instead of vanishing.

    `persist` asks for the project's approved /persist disk (vm/persist.py).
    It is honoured only for a top-level turn (`push_workspace`) of a project
    the operator approved, never for an ephemeral (incognito) envelope; the
    caller passes False for anything else. Off by default: fail closed.

    A nested turn passes an op_id already carrying the operation's Budget; a
    top-level turn's op_id is fresh, and it inherits the operation's Budget if one
    is in scope (contextvar) so every turn in one operation meters into one Budget."""
    op_id = op_id or f"guest:{conversation_id}"
    # which box runs this turn: the shared box unless boxes are enabled and
    # the project's profile gives it its own. Resolved BEFORE anything is
    # registered, so a refusal (BoxCapError) leaks no budget or token.
    box = await boxes.for_project(active_slug)
    # a joined box (placement "join") runs one project's turns at a time so its
    # egress is attributed to the right project; nothing awaits between here
    # and bind_op below, so the slot cannot be taken in between
    box = await boxes.wait_turn_slot(box, active_slug)
    guest_vm = boxes.controller(box)  # the shared box: lifecycle.vm, as before
    holds_ws = bool(push_workspace and active_slug)
    # What this turn has registered, so the ONE finally below undoes exactly that
    # however the generator exits. A flag is set before the call it guards (an
    # undo of something never done is a no-op; a missed undo is a leak) except
    # `pinned`, which counts only a boot that succeeded. A failed boot used to
    # leave the workspace hold, budget, envelope, op token and egress attribution
    # behind, and every later turn on the project then lost its workspace push.
    owns_budget = registered_env = registered_token = bound = pinned = False
    ws_held = owns_ws = staged_done = saw_final = False
    persist_fact, persist_gen = None, 0
    held_final = None       # the owner's `final`, held until its edits are applied
    loop = asyncio.get_running_loop()
    s = None
    try:
        owns_budget = budget_mod.get(op_id) is None
        if owns_budget:
            # share the operation's Budget object if we're inside one (nested), else
            # open this operation's own — release() later drops only this id's alias.
            inherited = budget_mod.current()
            budget_mod.register(op_id, inherited or Budget(
                settings.max_op_input_tokens, settings.max_op_output_tokens))
        if envelope is not None:
            registered_env = True
            broker.register_turn(envelope)
        # The turn's capability token: an op_id names a turn, this proves one. Minted
        # here because this is the single place op_ids are handed out, registered
        # host-side, and shipped exactly once in the spec below — the guest keeps it
        # task-local (turnctx) so concurrent turns in one guest cannot use each
        # other's. Registered unconditionally, not just when there is an envelope,
        # because `model_call` is gated on it too and a no-envelope turn still spends.
        op_token = secrets.token_urlsafe(24)
        registered_token = True
        broker.register_token(op_id, op_token)
        if boxes.enabled():
            bound = True
            boxes.bind_op(op_id, box, active_slug)      # the gateway refuses it from any other box
        # /persist is a shared-box mechanism (retired in favour of service boxes)
        want_persist = bool(persist and holds_ws and box.is_shared
                            and not (envelope is not None and envelope.ephemeral)
                            and await persist_mod.approved(active_slug))
        spec = {
            "conversation_id": conversation_id,
            "system_prompt": system_prompt,
            "history": history,
            "rules": rules,
            "tool_specs": tool_specs or [],
            "read_only": list(read_only or []),
            "op_id": op_id,
            # ...and the secret that makes the op_id above mean something. Every
            # model_call and tool_broker_call must carry it back or the gateway
            # refuses: without it, guessing a live op_id was enough to act as that
            # turn (see broker._op_tokens).
            "op_token": op_token,
            "gateway_port": settings.vm_vsock_port,
            "model_name": model_name,
            "base_url": base_url,
            "self_check": self_check,
            # voice turns skip the second-pass rules rewrite — the streamed text was
            # already spoken aloud, so rewriting it costs a model call and changes
            # nothing the operator will hear
            "rewrite_rules": rewrite_rules,
            "inject_rules": inject_rules,
            "max_iterations": max_iterations,
            "config": config_snapshot(),
            "active_slug": active_slug,
            # WP5: whether this turn is addressable. On, the guest loop drains its
            # inbox over the broker between iterations; off, it never asks and pays
            # nothing. Off for anything with no identity worth writing to.
            "inbox": inbox,
            # the project's files a tainted turn wrote (backend/taintpaths.py): the
            # guest reports a read of one, so the turn is tainted like a web_read
            "tainted_paths": taintpaths.paths(active_slug) if active_slug else [],
        }
        await guest_vm.acquire()      # boot + pin the guest for this turn's life
        pinned = True                 # (a failed acquire took no pin: release would steal one)
        # Only now is the hold taken and the copy built: everything above can
        # fail (no image, boot timeout, no KVM) and none of it may leave a hold
        # behind. First-in pushes a fresh copy; joiners reuse it (no await between
        # the check and the set).
        if holds_ws:
            ws_held = True
            owns_ws = acquire_workspace(active_slug)
        if owns_ws:
            # ship the workspace so the in-guest file tools work on a copy; the
            # guest's write buffer comes back after the turn. Built inline, not in a
            # thread: a joiner that takes its hold while this awaited would reach
            # the guest before the copy does (a stalled loop for a big project is
            # the smaller harm; a joiner needs the shipped-ack a thread would add).
            spec["workspace_tar_b64"] = _workspace_b64(active_slug)
        if want_persist:
            # attach + mount BEFORE the turn starts (the guest is pinned, so the
            # reaper can't scrub between here and the release in finally). The
            # guest reads this to tell the agent /persist exists; absent, the
            # agent is told nothing.
            persist_fact = await persist_mod.attach_for_turn(active_slug)
            if persist_fact:
                persist_gen = persist_mod.generation()   # no await since attach
                spec["persist"] = persist_fact
        # the box's transport: vsock (executor connect: uvloop's sock_connect
        # chokes on an AF_VSOCK (cid, port) tuple) or a docker box's unix
        # socket. Either way a connected non-blocking socket.
        s = await box.transport.connect(GUEST_RUNTURN_PORT)
        await loop.sock_sendall(s, (json.dumps(spec) + "\n").encode())
        buf = bytearray()
        while True:
            # once the answer is held the write buffer is due within seconds: a
            # guest that stalls there must not hold the answer forever
            line = await _recv_line(loop, s, buf, MAX_LINE,
                                    RPC_TIMEOUT if held_final is not None else None)
            if line is None:
                break                     # the guest closed the connection
            if not line.strip():
                continue
            ev = json.loads(line)
            kind = ev.get("type")
            if kind == "staged":
                # the guest's write buffer, sent AFTER `final` — apply it
                # host-side (writes.apply_write: secret refusal + advisory diff
                # gate) and don't surface it to the caller. Only the workspace
                # owner receives this; joiner/nested edits ride the shared
                # buffer. The stream ends when the guest closes.
                if owns_ws:
                    staged_done = True
                    note = await _apply_staged(active_slug, ev, op_id)
                    if note and held_final is not None:
                        held_final = {**held_final,
                                      "content": (held_final.get("content") or "") + note}
                continue
            if kind == "turn_stats":
                # the loop's per-turn counters (RUNS-08): recorded here, never
                # surfaced. An incognito turn leaves no row.
                if not (envelope is not None and envelope.ephemeral):
                    await turnstats.record(conversation_id, op_id, ev,
                                           box_id=getattr(box, "id", None))
                continue
            if kind == "final":
                saw_final = True
                if owns_ws and not staged_done:
                    # the owner's edits are still to come: hold the answer so a
                    # refused or failed file can be told in it (it is what the
                    # operator reads and what the next turn's history keeps)
                    held_final = ev
                    continue
            yield ev
        if not saw_final:
            raise GuestStreamError(
                "guest closed the connection mid-turn (no final answer: the guest "
                "crashed, ran out of memory or its VM was reaped)")
        if held_final is not None:
            if not staged_done:
                # `final` came but the edits did not: fetch them while the guest is up
                staged_done = True
                res = await _rescue(active_slug, op_id)
                held_final = {**held_final, "content": (held_final.get("content") or "") + (
                    workspace_xfer.describe_unapplied(res) if res is not None
                    else workspace_xfer.LOST_NOTE)}
            yield held_final
    finally:
        # a stop must also stop what the guest had brokered to the host (a
        # spawn_agent child, a research job, a model stream): nothing else will
        getattr(broker, "cancel_inflight", lambda _op: None)(op_id)
        if s is not None:
            s.close()
        last_out = False
        try:
            if owns_ws and not staged_done:
                # stopped (or failed) before the edits came home: bring them now,
                # while the copy is still whole. The next turn's unpack would wipe
                # them, and the history would still say the writes succeeded.
                # Before the hold is released, so a newcomer's push cannot land first.
                await _rescue(active_slug, op_id)
        finally:
            if ws_held:
                last_out = release_workspace(active_slug)
            try:
                if last_out and not owns_ws:
                    # last one out of a shared workspace, and the owner's turn-end
                    # pack already happened (or never will): sweep the buffer home.
                    # Repeat applies of the same bytes are idempotent.
                    try:
                        await pull_writes(active_slug)
                    except Exception:  # noqa: BLE001 — best-effort sweep
                        pass
                if persist_fact:
                    # last one out unmounts + unplugs, while the guest is still pinned
                    try:
                        await asyncio.wait_for(
                            persist_mod.release_for_turn(active_slug, persist_gen), RPC_TIMEOUT)
                    except Exception as e:  # noqa: BLE001 — the unwind must reach the pin
                        log.warning("persist release for %s failed: %s: %s",
                                    active_slug, type(e).__name__, e)
            finally:
                if pinned:
                    guest_vm.release()
                if bound:
                    boxes.unbind_op(op_id)
                if registered_token:
                    broker.release_token(op_id)
                if registered_env:
                    broker.release_turn(op_id)
                if owns_budget:
                    budget_mod.release(op_id)


def _workspace_b64(slug: str) -> str:
    return base64.b64encode(workspace_xfer.build_merged_tar(slug)).decode()


async def _apply_staged(slug: str, ev: dict, op_id: str | None) -> str:
    """Apply the guest's `staged` buffer host-side. Returns what to tell the
    reader about files that did not land ('' when all did)."""
    try:
        res = await workspace_xfer.apply_guest_writes(
            slug, base64.b64decode(ev.get("tar_b64") or ""), op_id)
    except Exception as e:  # noqa: BLE001 — a corrupt buffer must not fail a finished turn
        log.warning("could not apply the guest's edits for %s: %s: %s",
                    slug, type(e).__name__, e)
        return workspace_xfer.LOST_NOTE
    return workspace_xfer.describe_unapplied(res)


async def _rescue(slug: str, op_id: str | None) -> dict | None:
    """Pull the guest's write buffer and apply it (a turn that stopped early, or
    a guest that closed before sending it). None when the guest cannot be reached."""
    try:
        return await asyncio.wait_for(pull_writes(slug, op_id), RESCUE_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — a dead guest must not block the unwind
        log.warning("could not bring the guest's edits home for %s: %s: %s",
                    slug, type(e).__name__ or "error", e)
        return None


async def _recv_line(loop, sock, buf: bytearray, limit: int, timeout=None) -> bytes | None:
    """The next newline-terminated line from `sock` (leftover bytes stay in
    `buf`), or None when the peer closed (or, with a `timeout`, went quiet).
    Linear in the line's size, and a line over `limit` bytes is an error: the
    old `buf += chunk; while b"\\n" not in buf` copied the whole line on every
    chunk and had no bound, so 16 MB with no newline cost 550 MB and seconds."""
    start = 0
    while True:
        i = buf.find(b"\n", start)
        if i >= 0:
            line = bytes(buf[:i])
            del buf[:i + 1]
            return line
        start = len(buf)
        if start > limit:
            raise GuestStreamError(f"guest sent a line of over {limit:,} bytes")
        try:
            if timeout is None:
                chunk = await loop.sock_recv(sock, 65536)
            else:
                chunk = await asyncio.wait_for(loop.sock_recv(sock, 65536), timeout)
        except asyncio.TimeoutError:
            return None
        if not chunk:
            return None
        buf += chunk


async def _guest_rpc(spec: dict, box=None) -> dict | None:
    """One short request/response to a box's run-turn server (prime / pull /
    ps). `box` None = the shared box."""
    loop = asyncio.get_running_loop()
    s = await (box or boxes.shared()).transport.connect(GUEST_RUNTURN_PORT)
    try:
        await loop.sock_sendall(s, (json.dumps(spec) + "\n").encode())
        line = await _recv_line(loop, s, bytearray(), MAX_LINE)
        return json.loads(line) if line is not None else None
    finally:
        s.close()


async def _pinned_rpc(spec: dict, box) -> dict | None:
    """prime/pull against a project box boot + pin it for the call (it may be
    stopped or reaped between turns). The shared box keeps today's contract:
    the caller's operation already has it up. A guest that never answers ends
    the call after RPC_TIMEOUT rather than the caller's stop."""
    if box.is_shared:
        return await asyncio.wait_for(_guest_rpc(spec), RPC_TIMEOUT)
    ctl = boxes.controller(box)
    await ctl.acquire()
    try:
        return await asyncio.wait_for(_guest_rpc(spec, box), RPC_TIMEOUT)
    finally:
        ctl.release()


async def prime_workspace(slug: str) -> None:
    """Push ONE fresh workspace copy for an operation whose turns will reuse it
    (orchestrator leaves fan out concurrently on one project — priming once up
    front avoids each leaf racing a fresh unpack of the shared guest dir).
    Callers hold the slug via acquire_workspace and prime only when first in."""
    tar_b64 = _workspace_b64(slug)
    await _pinned_rpc({"mode": "prime", "active_slug": slug,
                       "workspace_tar_b64": tar_b64}, await boxes.for_project(slug))


async def pull_writes(slug: str, op_id: str | None = None) -> dict | None:
    """Pull the operation's accumulated guest write buffer and apply it host-side
    (secret refusal + advisory diff gate) — the counterpart to prime_workspace.
    Returns what apply_guest_writes did, or None when the guest had nothing."""
    ev = await _pinned_rpc({"mode": "pull", "active_slug": slug},
                           await boxes.for_project(slug))
    if ev and ev.get("type") == "staged":
        return await workspace_xfer.apply_guest_writes(
            slug, base64.b64decode(ev.get("tar_b64") or ""), op_id)
    return None


async def box_rpc(box, spec: dict) -> dict | None:
    """Public one-shot RPC to a box's run-turn server, e.g. {"mode": "ps"}
    (WP4's poller). Does not boot the box: a stopped box raises OSError."""
    return await _guest_rpc(spec, box)
