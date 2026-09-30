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
from ..agent import imageresult
from ..agent.tools import registry

# an image a host tool returns rides the broker reply inline; same ceiling the
# loop applies before showing it to the model
_IMG_WIRE_CAP = 4_500_000


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
    # the op that delegated to this one (spawn_agent, deploy_agents, a plan
    # item...), read host-side from the operation scope when the turn opens
    # (vm.turn.run_agent_turn), never from the guest. release_turn hands this
    # turn's taint up to it: the parent is about to read the child's report.
    parent_op: str | None = None


_envelopes: dict[str, TurnEnvelope] = {}


def register_turn(env: TurnEnvelope) -> None:
    proj = env.active_project
    if proj and not any(e.active_project == proj for e in _envelopes.values()):
        # nothing live on this project: a taint from an earlier turn is not this
        # turn's business (see _dirty_projects)
        _dirty_projects.discard(proj)
    _envelopes[env.op_id] = env
    if env.parent_op and env.parent_op in _tainted:
        # a child starts as tainted as the parent that briefed it: its task text
        # may be the parent's paraphrase of a web page
        _taint_op(env.op_id, _nav_tainted.get(env.parent_op))
    # attribute the guest's egress (which carries no op_id) to this turn's project
    from .. import egress
    egress.set_context(env.active_project, env.op_id, env.conversation_id)


def release_turn(op_id: str) -> None:
    env = _envelopes.pop(op_id, None)
    parent = env.parent_op if env is not None else None
    if parent and parent != op_id and op_id in _tainted and parent in _envelopes:
        # a child that read untrusted content hands the taint up: its report is
        # about to become a tool result in the parent's context. (A parent that
        # already ended is skipped: nothing would ever clear the entry.)
        mark_tainted(parent, _nav_tainted.get(op_id))
        _from_children[parent] = _from_children.get(parent, 0) + 1
    _tainted.discard(op_id)          # forget the turn's taint history too
    _nav_tainted.pop(op_id, None)
    _taint_src.pop(op_id, None)
    _from_children.pop(op_id, None)
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
                              "research",
                              # a computer's screen and its shell output are
                              # whatever is on that machine — a web page, a
                              # chat window, a file somebody sent. Every desk
                              # result is untrusted, input verbs included (they
                              # return the post-action screenshot).
                              "desk_screenshot", "desk_click", "desk_move",
                              "desk_scroll", "desk_type", "desk_key",
                              "desk_open", "desk_shell", "desk_wait",
                              "desk_drag",
                              # the operator's browser (jav3-browser): pages
                              # are web content, exactly like web_read
                              "browser_open_tab", "browser_navigate",
                              "browser_read_page", "browser_click",
                              "browser_type", "browser_scroll",
                              "browser_scroll_to_element",
                              "browser_screenshot_tab", "browser_close_tab",
                              "browser_list_tabs", "browser_select",
                              "browser_hover", "browser_key", "browser_back",
                              # /local: files and command output from the
                              # operator's own machine — a cloned repo's README
                              # is as attacker-authorable as a web page
                              "local_read_file", "local_list_files",
                              "local_search", "local_shell",
                              "local_write_file", "local_edit_file",
                              # WP3: a service's journal is whatever the
                              # (agent-written, network-facing) service printed
                              "service_logs"})

# ...and whole families by prefix. The projector verbs return text an MCP
# server wrote (backend/mcp.py: results are data, and tainted); a new verb
# added under the same prefix is covered without anyone remembering this list.
_UNTRUSTED_PREFIXES = ("projector_",)

# Tools that promote content INTO a trusted store the agent later relies on.
# memory_write is the one such store the guest can reach through the broker
# (git goes via the commit gate — operator-gated, not guest-brokered; file
# writes land direct but are scanned + advisory-flagged in writes.apply_write).
# A promotion made in a turn that has already consumed untrusted content is the
# laundering path this guards.
#
# journal_update is the second: project.md is loaded whole into every prompt and
# its summary feeds the all-projects rollup. In a tainted turn the handler tags
# the line [unverified] and assembly leaves it out (memory.strip_unverified).
_PROMOTION_TOOLS = frozenset({"memory_write", "journal_update"})

# Tools that write text into a trusted channel with no tag to hold it back: an
# agent definition's description rides every prompt (memory.agents_index) and
# its prompt runs unattended once spawned or scheduled. A turn that has read
# untrusted content does not get to write or rewrite one; the operator can, in
# the Agents tab, or ask again in a fresh message. (schedule_update needs no
# entry: its rows are created paused and wait for the operator's approval.)
_REFUSED_WHEN_TAINTED = frozenset({"create_agent"})

# op_ids that have consumed untrusted tool output this turn. The static memory
# rule (agent notes are approved:false until the operator promotes them) is the
# primary block; this ledger is the runtime half — it catches the promotion at
# the moment it happens and records the provenance on the result.
_tainted: set[str] = set()
# ...and, of those, the ones tainted by a screen or a page (desk_* / browser_*):
# op_id -> "desk" | "browser". Feeds runtime.nav_taint for memory_write.
_nav_tainted: dict[str, str] = {}
# ...and what tainted each one, in order: [(kind, detail)], kind one of
# memory.TAINT_KINDS ("web", "desk", "browser", "local", ...), detail the
# desk's name for "desk". Only labels the quarantine note; what is
# quarantined and refused does not depend on it.
_taint_src: dict[str, list[tuple[str, str | None]]] = {}
_WEB_TOOLS = frozenset({"web_read", "web_search", "read_and_summarize", "research"})
# Projects on which a turn became tainted since the project was last idle. A
# turn's own entry dies with it (release_turn), but the guest's write buffer can
# be pulled AFTER that (the commit gate flushes it): the files in it were still
# written by a tainted turn. workspace_xfer.apply_guest_writes reads this.
_dirty_projects: set[str] = set()
# op_id -> how many tainted children have ended under it (release_turn). The
# delegating call's result is annotated when this moved during the call.
_from_children: dict[str, int] = {}

_CHILD_TAINT_NOTE = (
    "\n\n[taint: an agent you delegated to read untrusted content (a web page, a "
    "search, a file, a message or a screen), so this report is derived from it. "
    "From here on this turn is treated like one that read the web: anything you "
    "save to memory is quarantined until the operator approves it, and "
    "unverified text does not become a standing rule.]")


def _nav_source(name: str) -> str | None:
    return ("desk" if name.startswith("desk_")
            else "browser" if name.startswith("browser_") else None)


def taint_kind(name: str) -> str | None:
    """The source kind an untrusted tool taints a turn with."""
    if name in _WEB_TOOLS:
        return "web"
    if name.startswith("local_"):
        return "local"
    if name == "service_logs":
        return "service"
    if name == "desk_shell":
        return "desk_shell"
    return _nav_source(name)


def _note_source(op_id: str, kind: str | None, detail: str | None = None) -> None:
    from .. import memory
    if kind not in memory.TAINT_KINDS:
        return
    if not (isinstance(detail, str) and 0 < len(detail) <= 64 and detail.isprintable()):
        detail = None
    got = _taint_src.setdefault(op_id, [])
    for i, (k, d) in enumerate(got):
        if k == kind:
            if d is None and detail:
                got[i] = (k, detail)          # the desk's name, learned late
            return
    if len(got) < 8:
        got.append((kind, detail))


def taint_sources(op_id: str) -> list[tuple[str, str | None]]:
    """What tainted this operation, in order: [(kind, detail)]."""
    return list(_taint_src.get(op_id, ()))


def classify_taint(name: str) -> str:
    return ("untrusted" if name in _UNTRUSTED_TOOLS
            or name.startswith(_UNTRUSTED_PREFIXES) else "trusted")


def op_tainted(op_id: str) -> bool:
    """Whether this operation has consumed untrusted tool output yet."""
    return op_id in _tainted


def _taint_op(op_id: str, source: str | None = None,
              detail: str | None = None) -> None:
    """The one place a turn joins the ledger. `source` is a memory.TAINT_KINDS
    kind (it labels the quarantine note); "desk" / "desk_shell" / "browser" also
    record that a screen or a page did it (_nav_tainted)."""
    _tainted.add(op_id)
    if source in ("desk", "desk_shell", "browser"):
        _nav_tainted.setdefault(op_id, "desk" if source == "desk_shell" else source)
    _note_source(op_id, source, detail)
    env = _envelopes.get(op_id)
    if env is not None and env.active_project:
        _dirty_projects.add(env.active_project)


def project_tainted(slug: str | None, *, consume: bool = False) -> bool:
    """Was a turn on this project tainted while it was live, or since the
    project was last idle? `consume` resets the since-idle half (one pull of the
    guest's write buffer takes it)."""
    if not slug:
        return False
    live = any(e.active_project == slug and e.op_id in _tainted
               for e in _envelopes.values())
    pending = slug in _dirty_projects
    if consume:
        _dirty_projects.discard(slug)
    return live or pending


async def taint_from_egress(att: dict, host: str | None = None) -> None:
    """The egress proxy just allowed a connection for `att` (its attribution):
    bytes from the network are about to enter the guest, so whatever the turns
    on that project print next (a curl, a git clone, a fetched page) is
    untrusted, and run_code has no broker hop to say so. Taints every live turn
    of the project, since with turns overlapping on one guest the proxy cannot
    tell which of them asked. A package registry does not count: `pip install`
    and `npm install` would otherwise taint every build."""
    from .. import egress
    if att.get("kind") not in ("shared", "project"):
        return          # a service box's own traffic is not a turn's; nor is an image build's
    if host and egress._host_matches(egress._norm(host), list(egress.IMAGE_BUILD_HOSTS)):
        return
    proj = att.get("project")
    ops = {att.get("op_id")} if att.get("op_id") else set()
    if proj and not egress.is_unattributed(proj):
        ops |= {e["op_id"] for e in egress.contexts_matching(
            lambda e: bool(e["op_id"]) and e["project"] == proj)}
    for op in ops:
        env = _envelopes.get(op)
        if env is None or op in _tainted:
            continue
        _taint_op(op, "web")
        try:
            from . import persist
            await persist.on_taint(env.active_project)
        except Exception:  # noqa: BLE001 — the proxy must never fail over this
            pass


def mark_tainted(op_id: str, source: str | None = None,
                 detail: str | None = None) -> None:
    """Stamp an operation untrusted from outside the name-based classifier.

    `classify_taint` decides from the tool NAME alone, which is right for
    web_read (every call returns attacker-authorable text) and wrong for
    inbox_fetch (an empty poll returns nothing, and polling happens every
    round). The inbox handler calls this only when a peer's words actually
    entered the turn — otherwise every turn in the system would come up
    tainted for having checked an empty mailbox.

    `source` (a memory.TAINT_KINDS kind: "desk", "browser", "peer", ...)
    labels what did it for the quarantine note, `detail` the desk's name;
    "desk" / "browser" also record that a screen or a page did it (see
    _nav_tainted)."""
    if op_id:
        _taint_op(op_id, source, detail)


async def broker_dispatch(op_id: str, name: str, args: dict,
                          call_id: str | None = None) -> dict:
    """Restore the turn's ambient context and run one host tool. Returns a
    structured {result, taint[, image]} so metadata can grow without a protocol
    change. `image` ({b64, mime, caption}) is present when the tool returned
    one: a host path means nothing to the guest, so the bytes travel inline
    and the guest registry re-attaches them (imageresult.with_inline).

    `call_id` is the model's id for this call as the guest loop saw it — a
    label for correlating with the chat stream's tool events, never trusted
    for anything else, so it is bounded and dropped if it is not a short
    string."""
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
    ok_id = (isinstance(call_id, str) and 0 < len(call_id) <= 128
             and call_id.isprintable())
    cidtok = runtime.tool_call_id.set(call_id if ok_id else None)
    # also restore the operation's budget id: a tool that itself runs a turn
    # (spawn_agent, deploy_agents) must resolve THIS operation's Budget so the
    # nested loop meters into it and knows it is nested (shares the guest).
    optok = budget_mod.active_op_id.set(env.op_id)
    # a promotion is "laundering" only if untrusted content was consumed BEFORE
    # it — evaluate against the ledger as it stood on entry
    launder = name in _PROMOTION_TOOLS and op_id in _tainted
    sources_then = taint_sources(op_id)
    was_tainted = op_id in _tainted
    # persist the taint onto the written note (not just the in-turn result): the
    # handler reads this contextvar and stamps `taint: untrusted` into frontmatter.
    taint_tok = runtime.write_taint.set("untrusted") if launder else None
    nav_tok = (runtime.nav_taint.set(_nav_tainted[op_id])
               if launder and op_id in _nav_tainted else None)
    try:
        # tier-4 hook (pre-dispatch): policy / deterministic diff-gate on
        # (name, args, env) — halt-for-human or reject goes here.
        # The conversation's permission mode (permissions.py) is the first:
        # the guest asks it before an in-guest write/run (GATE_OP), and a
        # brokered write-type tool passes it here.
        from .. import permissions
        if name == permissions.GATE_OP:
            return {"result": await permissions.gate_from_guest(args),
                    "taint": "trusted"}
        blocked = await permissions.gate(name, args)
        if blocked is not None:
            return {"result": blocked, "taint": "trusted"}
        if name in _REFUSED_WHEN_TAINTED and was_tainted:
            from .. import memory
            await memory.audit("memory_refused", "warn",
                               f"{name} refused: this turn had read untrusted content",
                               {"tool": name})
            return {"result": (
                f"error: refused — this turn read untrusted content (a web page, a search, "
                f"a file, a message or a screen), and {name} writes text that rides future "
                "prompts and runs unattended. Tell the operator what you wanted; they can do "
                "it in the Agents tab, or ask you again in a new message."),
                "taint": "trusted"}
        kids_before = _from_children.get(op_id, 0)
        result = await registry.dispatch(name, args)
        result, img = imageresult.split(result)
        # tier-4 (post-dispatch): stamp taint into the ledger, and mark a
        # laundering promotion on the result the model sees.
        if classify_taint(name) == "untrusted":
            comp = args.get("computer") if isinstance(args, dict) else None
            _taint_op(op_id, taint_kind(name),
                      comp if name.startswith("desk_") else None)
        if op_id in _tainted and not was_tainted:
            # this call is what tainted the turn (a web read, or a peer message
            # via mark_tainted). Its project's /persist goes read-only at the
            # QEMU block layer NOW, before the untrusted text reaches the guest
            # — so nothing written after reading it can survive the session.
            from . import persist
            await persist.on_taint(env.active_project)
        if launder and not result.startswith("error:"):
            from .. import memory
            result += memory.quarantine_note(sources_then)
        if _from_children.get(op_id, 0) > kids_before and not result.startswith("error:"):
            result += _CHILD_TAINT_NOTE
        out = {"result": result, "taint": classify_taint(name)}
        wire = img.wire(_IMG_WIRE_CAP) if img is not None else None
        if wire is not None:
            out["image"] = wire
        return out
    finally:
        runtime.tool_call_id.reset(cidtok)
        budget_mod.active_op_id.reset(optok)
        if taint_tok is not None:
            runtime.write_taint.reset(taint_tok)
        if nav_tok is not None:
            runtime.nav_taint.reset(nav_tok)
        for v, tok in zip(vars_, tokens):
            v.reset(tok)
