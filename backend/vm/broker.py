"""Host tool broker for guest-run turns.

The guest loop can't run host-brokered tools itself — it sends a `tool_broker_call`
over vsock and the host runs it HERE, behind every existing gate. This is a THIN
pass-through to `registry.dispatch` (it never reimplements tool logic), so the
write scans, git-commit approval, SSRF guard, and secret substitution stay
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
    # agents/<slug>/memory is this turn's notes dir (own_memory agents only);
    # host-derived from the definition, restored into runtime.agent_memory
    memory_slug: str | None = None


_envelopes: dict[str, TurnEnvelope] = {}


def register_turn(env: TurnEnvelope) -> None:
    _envelopes[env.op_id] = env
    # attribute the guest's egress (which carries no op_id) to this turn's project
    from .. import egress
    egress.set_context(env.active_project, env.op_id, env.conversation_id)


def release_turn(op_id: str) -> None:
    _envelopes.pop(op_id, None)
    _tainted.discard(op_id)          # forget the turn's taint history too
    # ...and hand egress attribution back to whatever turn is still running, or
    # to nobody. Leaving it set meant a finished project kept policing the
    # guest's later traffic.
    from .. import egress
    egress.clear_context(op_id)


def get_turn(op_id: str) -> TurnEnvelope | None:
    return _envelopes.get(op_id)


# --- op_id capability tokens -------------------------------------------------
# An op_id names a turn; it does not PROVE one. They are deterministic
# (`chat:{cid}`, `guest:{cid}`), the guest supplies its own in every request,
# and one guest legitimately runs several turns at once — so a compromised
# guest holds a few real op_ids and can count to the rest. Presenting somebody
# else's made the gateway serve it: `broker_dispatch` restored the victim's
# envelope and the caller then acted as that turn, with its project pin, its
# web session, its artifact store and its Budget.
#
# The fix is a per-turn secret minted host-side, shipped once in the turn spec,
# and required on every request alongside the op_id. Binding to the CONNECTION
# instead does not work here: the guest opens a fresh short-lived vsock
# connection per model/tool call, and several concurrent turns in one guest
# share both the gateway and the guest's CID, so there is nothing about a
# connection that distinguishes them. The token is what does — it lives in the
# guest's per-turn `turnctx`, the same task-local that already keeps concurrent
# turns' op_ids apart.
#
# Honest limit: this stops op_id GUESSING, not a guest that can read another
# concurrent turn's task-local state. Anything with that reach already had the
# victim's op_id too, so the token does not make that case worse — it removes
# the case where no reach at all was needed.
_op_tokens: dict[str, str] = {}


def register_token(op_id: str, token: str) -> None:
    _op_tokens[op_id] = token


def release_token(op_id: str) -> None:
    _op_tokens.pop(op_id, None)


def verify_token(op_id, token) -> bool:
    """Whether this caller is entitled to act as `op_id`.

    Both arguments come straight off guest-supplied JSON, so both are guarded:
    a non-string op_id (a dict is even unhashable, so `.get` would raise) or a
    non-string token (int/list/dict/bool/non-ASCII would make compare_digest
    raise) is simply a miss. The raise fails closed — no dispatch, nothing
    written — but it kills the connection task with an unretrieved exception, so
    it is turned into a clean `unknown_op_id` here at the input rather than
    swallowed by a broadened except in the gateway.

    Fails closed on an unregistered op_id and on a missing token too, so a guest
    running older pushed code (or none) loses the ability to act rather than
    keeping the hole open. compare_digest because this is a secret comparison,
    even though a timing oracle over vsock is a stretch."""
    import hmac
    if not isinstance(op_id, str) or not isinstance(token, str):
        return False
    known = _op_tokens.get(op_id)
    if not known:
        return False
    return hmac.compare_digest(known, token)


def live_turns() -> list[TurnEnvelope]:
    """Every turn in flight right now.

    This registry is the ONLY host-side answer to "who is running", and it is
    already the thing a compromised guest cannot write to — which is why WP5's
    addressing reads it rather than trusting anything the guest says about its
    peers. Snapshot (a list, not the dict) because a caller awaits between
    reading it and using it, and turns finish constantly."""
    return list(_envelopes.values())


# --- tier-4: taint tracking ------------------------------------------------
# Tools whose output carries untrusted external content: a page, a search
# result, a research report is content an attacker may have authored. Anything
# derived from it is suspect until a human vets it.
_UNTRUSTED_TOOLS = frozenset({"web_read", "web_search", "read_and_summarize",
                              "research"})

# Tools that promote content INTO a trusted store the agent later relies on.
# memory_write is the one such store the guest can reach through the broker
# (git goes via the commit gate — operator-gated, not guest-brokered; file
# writes land direct but are scanned + advisory-flagged in writes.apply_write).
# A promotion made in a turn that has already consumed untrusted content is the
# laundering path this guards.
_PROMOTION_TOOLS = frozenset({"memory_write"})

# op_ids that have consumed untrusted tool output this turn. The static memory
# rule (agent notes are approved:false until the operator promotes them) is the
# primary block; this ledger is the runtime half — it catches the promotion at
# the moment it happens and records the provenance on the result.
_tainted: set[str] = set()

_PROMOTION_QUARANTINE_NOTE = (
    "\n\n[taint: this write happened in a turn that already consumed untrusted "
    "external content (web/research). It is quarantined — stored but NOT binding "
    "on future turns until the operator reviews and approves it. Do not rely on "
    "it as an established fact this turn.]")


def classify_taint(name: str) -> str:
    return "untrusted" if name in _UNTRUSTED_TOOLS else "trusted"


def op_tainted(op_id: str) -> bool:
    """Whether this operation has consumed untrusted tool output yet."""
    return op_id in _tainted


def mark_tainted(op_id: str) -> None:
    """Stamp an operation untrusted from outside the name-based classifier.

    `classify_taint` decides from the tool NAME alone, which is right for
    web_read (every call returns attacker-authorable text) and wrong for
    inbox_fetch (an empty poll returns nothing, and polling happens every
    round). The inbox handler calls this only when a peer's words actually
    entered the turn — otherwise every turn in the system would come up
    tainted for having checked an empty mailbox."""
    if op_id:
        _tainted.add(op_id)


async def broker_dispatch(op_id: str, name: str, args: dict) -> dict:
    """Restore the turn's ambient context and run one host tool. Returns a
    structured {result, taint} so metadata can grow without a protocol change."""
    env = _envelopes.get(op_id)
    if env is None:
        return {"result": f"error: broker has no turn context for op_id {op_id!r}",
                "taint": "trusted"}
    vars_ = (runtime.web_session, runtime.ephemeral, runtime.artifact_slug,
             runtime.event_chan, runtime.active_project, runtime.conversation_id,
             runtime.agent_memory)
    vals = (env.web_session, env.ephemeral, env.artifact_slug, env.event_chan,
            env.active_project, env.conversation_id, env.memory_slug)
    tokens = [v.set(val) for v, val in zip(vars_, vals)]
    # also restore the operation's budget id: a tool that itself runs a turn
    # (spawn_agent, deploy_agents) must resolve THIS operation's Budget so the
    # nested loop meters into it and knows it is nested (shares the guest).
    optok = budget_mod.active_op_id.set(env.op_id)
    # a promotion is "laundering" only if untrusted content was consumed BEFORE
    # it — evaluate against the ledger as it stood on entry
    launder = name in _PROMOTION_TOOLS and op_id in _tainted
    was_tainted = op_id in _tainted
    # persist the taint onto the written note (not just the in-turn result): the
    # handler reads this contextvar and stamps `taint: untrusted` into frontmatter.
    taint_tok = runtime.write_taint.set("untrusted") if launder else None
    try:
        # tier-4 hook (pre-dispatch): policy / deterministic diff-gate on
        # (name, args, env) — halt-for-human or reject goes here.
        result = await registry.dispatch(name, args)
        # tier-4 (post-dispatch): stamp taint into the ledger, and mark a
        # laundering promotion on the result the model sees.
        if classify_taint(name) == "untrusted":
            _tainted.add(op_id)
        if op_id in _tainted and not was_tainted:
            # this call is what tainted the turn (a web read, or a peer message
            # via mark_tainted). Its project's /persist goes read-only at the
            # QEMU block layer NOW, before the untrusted text reaches the guest
            # — so nothing written after reading it can survive the session.
            from . import persist
            await persist.on_taint(env.active_project)
        if launder and not result.startswith("error:"):
            result += _PROMOTION_QUARANTINE_NOTE
        return {"result": result, "taint": classify_taint(name)}
    finally:
        budget_mod.active_op_id.reset(optok)
        if taint_tok is not None:
            runtime.write_taint.reset(taint_tok)
        for v, tok in zip(vars_, tokens):
            v.reset(tok)
