"""Host tool broker for guest-run turns.

The guest loop can't run host-brokered tools itself — it sends a `tool_broker_call`
over vsock and the host runs it HERE, behind every existing gate. This is a THIN
pass-through to `registry.dispatch` (it never reimplements tool logic), so the
staging quarantine, git-commit approval, SSRF guard, and secret substitution stay
authoritative host-side. It is also the single chokepoint every guest tool call
crosses — the natural home for the tier-4 controls. Their hook points are marked
below (pre-dispatch policy / diff-gate; post-dispatch taint stamp / scrub) so they
slot in without reshaping the protocol.

The turn's context envelope is registered host-side by op_id (register_turn) and
resolved here — the guest never carries it, so a compromised guest can't forge
active_project / web_session / ephemeral.
"""
from dataclasses import dataclass

from .. import runtime
from ..agent import budget as budget_mod
from ..agent.tools import registry


@dataclass
class TurnEnvelope:
    op_id: str
    conversation_id: int | None = None
    active_project: str | None = None
    artifact_slug: str | None = None
    web_session: str | None = None
    ephemeral: bool = False
    event_chan: str | None = None


_envelopes: dict[str, TurnEnvelope] = {}


def register_turn(env: TurnEnvelope) -> None:
    _envelopes[env.op_id] = env


def release_turn(op_id: str) -> None:
    _envelopes.pop(op_id, None)


def get_turn(op_id: str) -> TurnEnvelope | None:
    return _envelopes.get(op_id)


# --- tier-4 seam: per-tool result taint. Trivial now (tools that pull untrusted
# external content are UNTRUSTED); later this becomes full taint propagation that
# rides the result through summarize/memory and blocks untrusted->trusted promotion.
_UNTRUSTED_TOOLS = frozenset({"web_read", "web_search", "read_and_summarize", "research"})


def classify_taint(name: str) -> str:
    return "untrusted" if name in _UNTRUSTED_TOOLS else "trusted"


async def broker_dispatch(op_id: str, name: str, args: dict) -> dict:
    """Restore the turn's ambient context and run one host tool. Returns a
    structured {result, taint} so metadata can grow without a protocol change."""
    env = _envelopes.get(op_id)
    if env is None:
        return {"result": f"error: broker has no turn context for op_id {op_id!r}",
                "taint": "trusted"}
    vars_ = (runtime.web_session, runtime.ephemeral, runtime.artifact_slug,
             runtime.event_chan)
    vals = (env.web_session, env.ephemeral, env.artifact_slug, env.event_chan)
    tokens = [v.set(val) for v, val in zip(vars_, vals)]
    # also restore the operation's budget id: a tool that itself runs a turn
    # (spawn_agent, deploy_agents) must resolve THIS operation's Budget so the
    # nested loop meters into it and knows it is nested (shares the guest).
    optok = budget_mod.active_op_id.set(env.op_id)
    try:
        # tier-4 hook (pre-dispatch): policy / deterministic diff-gate on
        # (name, args, env) — halt-for-human or reject goes here.
        result = await registry.dispatch(name, args)
        # tier-4 hook (post-dispatch): taint stamping / result scrub / egress
        # volume accounting goes here, keyed off `name` + `env`.
        return {"result": result, "taint": classify_taint(name)}
    finally:
        budget_mod.active_op_id.reset(optok)
        for v, tok in zip(vars_, tokens):
            v.reset(tok)
