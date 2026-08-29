"""Inter-agent communication (WP5) — and the two run-tree defects beside it.

Before this there was no channel at all: a parent handed a child one task string
at spawn, a child handed a parent one report at the end, and siblings never
heard anything. These tests pin the four properties the design is built on:

  * identity is derived HOST-side from the turn envelope, so a compromised guest
    has no sender to forge;
  * the message is a DB row, so it survives a restart;
  * nothing is silently dropped — a failed drain leaves the message unclaimed,
    and a claim writes the words into the recipient's transcript before the
    recipient has seen them;
  * delivery is pulled by the recipient's loop between iterations, which is the
    only direction that works: the host has no inbound path to a running guest.

Offline throughout. Nothing here needs a model or an API key — `model` and
`registry.dispatch` are substituted, so this file exercises the real seams
rather than the real network.
"""
import asyncio
import contextvars
import json
import socket

import pytest

from backend import agentmsg, runtime
from backend.agent import loop as loop_mod
from backend.agent.tools import registry
from backend.db import get_db, init_db, open_conversation
from backend.vm import broker
from backend.vm.gateway_server import handle_conn


# --- fixtures / helpers ------------------------------------------------------

async def _conv(db, *, title="t", agent=None, kind="chat") -> int:
    """A conversation built the way production builds one.

    This used to INSERT the row by hand, `agent_slug` included — which is
    exactly how it hid the fact that no agent RUN ever set that column: the
    tests asserted against a shape only the tests produced. Everything goes
    through `open_conversation` now, so the helper can no longer manufacture a
    binding the real code paths never make."""
    return await open_conversation(db, project=None, title=title, kind=kind,
                                   agent=agent)


def _agent_file(tmp_env, slug: str) -> None:
    """A roster entry on disk. An agent slug is only an address if the roster
    knows it or a turn is running under it — otherwise a typo becomes a message
    nobody will ever claim."""
    d = tmp_env / "agents" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "AGENT.md").write_text(f"---\nname: {slug}\ndescription: d\n---\nYou are {slug}.")


class _Envelope:
    """Register a turn the way `guest_turn` does — envelope AND capability token.

    The token is the half that makes the op_id unforgeable, so a helper that
    registered only the envelope would let every test pass while the gateway
    accepted anybody's op_id. `token` is exposed so a test can present the wrong
    one on purpose."""

    def __init__(self, op_id, cid, project=None, ephemeral=False):
        self.env = broker.TurnEnvelope(op_id=op_id, conversation_id=cid,
                                       active_project=project, ephemeral=ephemeral)
        self.token = f"tok-{op_id}"

    def __enter__(self):
        broker.register_turn(self.env)
        broker.register_token(self.env.op_id, self.token)
        return self

    def __exit__(self, *exc):
        broker.release_turn(self.env.op_id)
        broker.release_token(self.env.op_id)


async def _gateway_call(op_id: str, name: str, args: dict, *,
                        token: str | None = None) -> dict:
    """One `tool_broker_call` exactly as a guest makes it: over a socket, served
    on a task that does NOT inherit this test's contextvars — which is the whole
    reason sender identity has to come off the host-side envelope.

    `token` defaults to this op's real one so the ordinary tests read normally;
    pass it explicitly to play an attacker holding somebody else's op_id."""
    if token is None:
        token = f"tok-{op_id}"
    loop = asyncio.get_running_loop()
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    server = asyncio.create_task(handle_conn(loop, b), context=contextvars.Context())
    try:
        await loop.sock_sendall(a, (json.dumps(
            {"op": "tool_broker_call", "op_id": op_id, "op_token": token,
             "name": name, "args": args}) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = await asyncio.wait_for(loop.sock_recv(a, 65536), timeout=10)
            if not chunk:
                break
            data += chunk
        return json.loads(data.split(b"\n", 1)[0])
    finally:
        a.close()
        await asyncio.wait_for(server, timeout=5)


class _Scripted:
    """A model that emits `rounds` tool-call rounds then finishes. Records the
    messages array it was handed each round, which is where a delivered inbox
    message has to show up."""

    def __init__(self, rounds=()):
        self.rounds = list(rounds)
        self.call = 0
        self.seen = []

    async def complete(self, messages, tools=None, **kw):
        self.seen.append([dict(m) for m in messages])
        if self.call < len(self.rounds):
            calls = [{"id": f"c{self.call}", "type": "function",
                      "function": {"name": self.rounds[self.call], "arguments": "{}"}}]
            self.call += 1
            yield {"type": "message", "content": "", "tool_calls": calls, "usage": None}
        else:
            yield {"type": "message", "content": "done", "tool_calls": [], "usage": None}


# --- sender identity ---------------------------------------------------------

async def test_sender_identity_is_the_envelope_not_the_arguments(tmp_env, monkeypatch):
    """The security property. `send_message` takes no "from": the host resolves
    the sender from the op_id's TurnEnvelope, which the guest never carries. A
    guest that lies about who it is has nothing to lie WITH."""
    await init_db()
    db = await get_db()
    try:
        alice = await _conv(db, title="alice", agent="alice")
        bob = await _conv(db, title="bob", agent="bob")
    finally:
        await db.close()

    with _Envelope("op-alice", alice), _Envelope("op-bob", bob):
        out = await _gateway_call("op-alice", "send_message",
                                  {"to": "bob", "message": "the config lives in vm/net/"})
    assert out["type"] == "broker_result"
    assert "sent to agent 'bob'" in out["result"]

    db = await get_db()
    try:
        async with db.execute(
            "SELECT from_conversation_id, from_label, to_agent_slug, body "
            "FROM agent_messages") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert len(rows) == 1
    assert rows[0]["from_conversation_id"] == alice, (
        "the sender was taken from somewhere other than the turn envelope")
    assert rows[0]["from_label"] == "alice"
    assert rows[0]["to_agent_slug"] == "bob"


async def test_a_forged_from_argument_is_rejected_outright(tmp_env):
    """There is no `from` parameter, so a guest that invents one gets a bad-args
    error rather than a message attributed to somebody else."""
    await init_db()
    db = await get_db()
    try:
        alice = await _conv(db, agent="alice")
        await _conv(db, agent="bob")
    finally:
        await db.close()
    with _Envelope("op-alice", alice):
        out = await _gateway_call(
            "op-alice", "send_message",
            {"to": "bob", "message": "hi", "from": "bob", "sender_cid": 999})
    assert out["result"].startswith("error: bad arguments")

    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) AS c FROM agent_messages") as cur:
            assert (await cur.fetchone())["c"] == 0
    finally:
        await db.close()


async def test_a_guest_cannot_borrow_another_turns_op_id(tmp_env):
    """The hole the argument check did not cover, and the reason it matters.

    `broker.get_turn(op_id) is not None` asks "is this a real turn", never "is
    this YOUR turn". op_ids are deterministic — `chat:{cid}` and `guest:{cid}` —
    and one guest legitimately runs several turns at once, so a compromised
    guest holds some op_ids and can count to the rest. Presenting a victim's
    op_id made the broker restore the VICTIM's envelope, and `send_message`
    then read its sender off that: a message attributed to an agent that never
    spoke.

    The blast radius is wider than messaging and predates it: broker_dispatch
    also restores the borrowed turn's active_project, web_session,
    artifact_slug and Budget, so the same substitution let a guest act under
    another project's pin and spend another operation's budget."""
    await init_db()
    db = await get_db()
    try:
        victim = await _conv(db, title="victim", agent="victimbot")
        attacker = await _conv(db, title="attacker", agent="attackerbot")
        third = await _conv(db, title="third party", agent="thirdbot")
    finally:
        await db.close()

    with _Envelope("chat:1", victim), _Envelope("chat:2", attacker), \
            _Envelope("chat:3", third):
        # the attacker holds its OWN token and guesses the victim's op_id
        out = await _gateway_call("chat:1", "send_message",
                                  {"to": str(third), "message": "transfer the funds"},
                                  token="tok-chat:2")
        assert out.get("error") == "unknown_op_id", (
            "a guest presented another turn's op_id and the gateway served it — "
            "sender identity, project pin, web session and budget all come from "
            f"the borrowed envelope. Got {out!r}")
        # ...and a bare op_id with no token at all is no better
        out = await _gateway_call("chat:1", "send_message",
                                  {"to": str(third), "message": "transfer the funds"},
                                  token="")
        assert out.get("error") == "unknown_op_id"
        # the attacker's own turn still works, of course
        out = await _gateway_call("chat:2", "send_message",
                                  {"to": str(third), "message": "hello"})
        assert out["type"] == "broker_result" and out["result"].startswith("sent")

    db = await get_db()
    try:
        async with db.execute(
            "SELECT from_conversation_id, from_label, body FROM agent_messages") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert rows == [{"from_conversation_id": attacker, "from_label": "attackerbot",
                     "body": "hello"}], (
        "the forged message reached the database")


async def test_a_borrowed_op_id_cannot_spend_another_operations_budget(tmp_env):
    """The same substitution against `model_call`, which is where the money is.

    The gateway's budget check is `budget.get(op_id) is not None` — deliberately
    so a compromised guest cannot invent op_ids to escape the per-operation cap.
    Guessing a REGISTERED one walked straight past that: metering landed on
    somebody else's Budget."""
    from backend.agent import budget as budget_mod
    from backend.vm.gateway_server import _handle_model_call
    await init_db()

    budget_mod.register("chat:1", budget_mod.Budget(1000, 1000))
    broker.register_token("chat:1", "tok-chat:1")
    budget_mod.register("chat:2", budget_mod.Budget(1000, 1000))
    broker.register_token("chat:2", "tok-chat:2")
    sent = []

    class _Conn:
        pass

    class _Loop:
        async def sock_sendall(self, conn, data):
            sent.append(json.loads(data))
    try:
        await _handle_model_call(_Loop(), _Conn(),
                                 {"op_id": "chat:1", "op_token": "tok-chat:2",
                                  "messages": []})
    finally:
        budget_mod.release("chat:1")
        budget_mod.release("chat:2")
        broker.release_token("chat:1")
        broker.release_token("chat:2")
    assert sent and sent[0].get("error") == "unknown_op_id", (
        "a guest metered a model call onto another operation's Budget by "
        f"presenting its op_id. Got {sent!r}")


def test_every_guest_side_gateway_call_carries_the_token():
    """Structural guard, in the spirit of test_guest_spec_parity.

    The token only protects anything if EVERY guest->host request carries it.
    There are two call sites today (model.py, the registry's broker shim) and
    nothing stops a third being added without it — that third one would simply
    stop working, and the obvious "fix" is to weaken the gateway. So: every dict
    literal under guest/ that names a gateway `op` must also set `op_token`."""
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "guest"
    sites = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            vals = {v.value for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)}
            if "op" in keys and vals & {"model_call", "tool_broker_call"}:
                sites.append((path.name, sorted(vals & {"model_call",
                                                        "tool_broker_call"})[0],
                              "op_token" in keys))
    assert sites, "the scan found no gateway call sites, so it checks nothing"
    missing = [(f, op) for f, op, ok in sites if not ok]
    assert not missing, (
        f"these guest->host requests name an op_id but carry no op_token, so "
        f"the gateway will refuse them: {missing}")


def test_verify_token_fails_closed(tmp_env):
    """An unregistered op, a missing token and a wrong token are all refusals —
    a guest running stale pushed code loses the ability to act rather than
    keeping the hole open for compatibility."""
    broker.register_token("op-x", "secret")
    try:
        assert broker.verify_token("op-x", "secret")
        assert not broker.verify_token("op-x", "wrong")
        assert not broker.verify_token("op-x", None)
        assert not broker.verify_token("op-x", "")
        assert not broker.verify_token("op-never-registered", "secret")
    finally:
        broker.release_token("op-x")
    assert not broker.verify_token("op-x", "secret")   # released with the turn


async def test_a_turn_with_no_envelope_cannot_send(tmp_env):
    """op_id pinning: an unregistered op_id never reaches the tool at all."""
    await init_db()
    out = await _gateway_call("op-nobody", "send_message", {"to": "x", "message": "y"})
    assert out["type"] == "error" and out["error"] == "unknown_op_id"


async def test_the_inbox_drain_takes_no_address_argument(tmp_env):
    """`inbox_fetch` is argument-free on purpose: the recipient is the turn's own
    conversation, resolved host-side, so there is no argument through which a
    compromised guest could read somebody else's mail."""
    await init_db()
    db = await get_db()
    try:
        alice = await _conv(db, agent="alice")
        bob = await _conv(db, agent="bob")
        await db.execute(
            "INSERT INTO agent_messages (from_conversation_id, from_label, "
            "to_conversation_id, body) VALUES (?,?,?,?)",
            (alice, "alice", bob, "for bob only"))
        await db.commit()
    finally:
        await db.close()

    with _Envelope("op-alice", alice):
        out = await _gateway_call("op-alice", "inbox_fetch", {"conversation_id": bob})
    assert out["result"].startswith("error: bad arguments")
    with _Envelope("op-alice", alice):
        out = await _gateway_call("op-alice", "inbox_fetch", {})
    assert out["result"] == "", "alice drained a message addressed to bob"


# --- delivery ----------------------------------------------------------------

async def test_a_running_agent_gets_the_message_on_its_next_round(tmp_env, monkeypatch):
    """End to end through the real loop: alice sends mid-turn, bob's ReAct loop
    drains between iterations and the words land in bob's message array."""
    await init_db()
    db = await get_db()
    try:
        alice = await _conv(db, agent="alice")
        bob = await _conv(db, agent="bob")
    finally:
        await db.close()

    model = _Scripted(rounds=["noop", "noop"])
    monkeypatch.setattr(loop_mod, "model", model)
    real_dispatch = registry.dispatch
    sent = []

    async def dispatch(name, args):
        if name == "inbox_fetch":
            # the recipient's drain: run it as the broker would, with bob's
            # identity restored from HIS envelope
            tok = runtime.conversation_id.set(bob)
            try:
                return await real_dispatch(name, args)
            finally:
                runtime.conversation_id.reset(tok)
        if name == "noop" and not sent:
            sent.append(1)          # alice sends while bob is between rounds
            tok = runtime.conversation_id.set(alice)
            try:
                return await real_dispatch(
                    "send_message",
                    {"to": str(bob), "message": "stop editing styles.css"})
            finally:
                runtime.conversation_id.reset(tok)
        return "ok"

    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())

    events = []
    with _Envelope("op-bob", bob):
        async for ev in loop_mod.run_turn(
                bob, "system", [{"role": "user", "content": "work"}],
                tools=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
                inbox=True):
            events.append(ev)

    assert any(e["type"] == "inbox" for e in events), (
        "the loop never surfaced the delivered message")
    body = "\n".join(m.get("content") or "" for m in model.seen[-1])
    assert "stop editing styles.css" in body
    assert "from alice" in body


async def test_inbox_off_means_the_loop_never_asks(tmp_env, monkeypatch):
    """The poll is a broker round-trip per round. A turn nobody can address —
    an incognito chat, a research node — must not pay for it."""
    await init_db()
    asked = []

    async def dispatch(name, args):
        asked.append(name)
        return "ok"

    monkeypatch.setattr(loop_mod, "model", _Scripted())
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())
    async for _ in loop_mod.run_turn(1, "s", [{"role": "user", "content": "x"}],
                                     tools=[]):
        pass
    assert "inbox_fetch" not in asked


async def test_the_inbox_rides_the_last_user_turn_not_after_it(tmp_env, monkeypatch):
    """Messages waiting when a turn STARTS join the history before the sandwich
    is assembled, so the standing-rules restatement still lands on the final
    user message. Appending afterwards would push the rules a message early —
    exactly the salience the restatement exists to buy (loop._assemble_messages)."""
    await init_db()
    db = await get_db()
    try:
        alice = await _conv(db, agent="alice")
        bob = await _conv(db, agent="bob")
        await db.execute(
            "INSERT INTO agent_messages (from_conversation_id, from_label, "
            "to_conversation_id, body) VALUES (?,?,?,?)",
            (alice, "alice", bob, "read docs/PLAN.md first"))
        await db.commit()
    finally:
        await db.close()

    model = _Scripted()
    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(loop_mod, "standing_rules_tail", lambda: "[RULES]")
    real = registry.dispatch

    async def dispatch(name, args):
        tok = runtime.conversation_id.set(bob)
        try:
            return await real(name, args)
        finally:
            runtime.conversation_id.reset(tok)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())

    async for _ in loop_mod.run_turn(
            bob, "system", [{"role": "user", "content": "work"}],
            tools=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
            inbox=True):
        pass
    last = model.seen[0][-1]
    assert last["role"] == "user"
    assert "read docs/PLAN.md first" in last["content"]
    assert "[RULES]" in last["content"], (
        "the inbox message displaced the standing-rules restatement off the "
        "last user turn")


# --- nothing is silently dropped --------------------------------------------

async def test_a_message_survives_a_restart(tmp_env):
    """The mistake this must not repeat: `bus.announce_job` publishes a one-off
    event and stores nothing, so reloading loses it. A message is a row."""
    await init_db()
    _agent_file(tmp_env, "bob")
    db = await get_db()
    try:
        alice = await _conv(db, agent="alice")
        bob = await _conv(db, agent="bob")
        await agentmsg.send(db, sender_cid=alice, to="bob", body="remember this")
    finally:
        await db.close()

    db = await get_db()            # a whole new connection: nothing in memory
    try:
        rows = await agentmsg.claim(db, cid=bob, agent_slug="bob")
    finally:
        await db.close()
    assert [r["body"] for r in rows] == ["remember this"]


async def test_a_failed_drain_loses_nothing(tmp_env, monkeypatch):
    """The drain swallows errors so a mail check can't end a turn doing
    unrelated work — which is only safe because a failed drain CLAIMS nothing
    and the next round tries again."""
    await init_db()
    db = await get_db()
    try:
        alice = await _conv(db, agent="alice")
        bob = await _conv(db, agent="bob")
        await agentmsg.send(db, sender_cid=alice, to=str(bob), body="still here")
    finally:
        await db.close()

    async def boom(name, args):
        raise RuntimeError("vsock died")
    monkeypatch.setattr(registry, "dispatch", boom)
    assert await loop_mod._drain_inbox() == ""

    db = await get_db()
    try:
        async with db.execute(
            "SELECT COUNT(*) AS c FROM agent_messages WHERE delivered_at IS NULL") as cur:
            assert (await cur.fetchone())["c"] == 1
    finally:
        await db.close()


async def test_delivery_writes_the_message_into_the_transcript(tmp_env):
    """The durability half of "never dropped". The claim marks the row
    delivered; if the reply carrying it back to the guest were lost it would be
    marked and unseen. Persisting it as a message row means the recipient's NEXT
    turn assembles it out of the DB — late, never gone."""
    await init_db()
    db = await get_db()
    try:
        alice = await _conv(db, agent="alice")
        bob = await _conv(db, agent="bob")
        await agentmsg.send(db, sender_cid=alice, to=str(bob), body="the API moved")
    finally:
        await db.close()

    with _Envelope("op-bob", bob):
        out = await _gateway_call("op-bob", "inbox_fetch", {})
    assert "the API moved" in out["result"]

    db = await get_db()
    try:
        async with db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ?",
            (bob,)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert rows and "the API moved" in rows[-1]["content"]
    assert "message from alice" in rows[-1]["content"]


async def test_two_turns_of_one_agent_cannot_both_claim_it(tmp_env):
    """A slug address is one message with one recipient. The claim is a single
    UPDATE ... RETURNING, so whichever statement lands first owns the row."""
    await init_db()
    _agent_file(tmp_env, "bob")
    db = await get_db()
    try:
        alice = await _conv(db, agent="alice")
        bob1 = await _conv(db, agent="bob")
        bob2 = await _conv(db, agent="bob")
        await agentmsg.send(db, sender_cid=alice, to="bob", body="one copy only")
        first = await agentmsg.claim(db, cid=bob1, agent_slug="bob")
        second = await agentmsg.claim(db, cid=bob2, agent_slug="bob")
    finally:
        await db.close()
    assert len(first) == 1 and second == []


async def test_a_message_to_an_idle_agent_waits_for_its_next_turn(tmp_env):
    """The addressee not being live is not an error — it is a mailbox. The
    sender is TOLD it was queued rather than delivered, so it never sits waiting
    for a reply that cannot come this turn."""
    await init_db()
    db = await get_db()
    try:
        alice = await _conv(db, agent="alice")
        bob = await _conv(db, agent="bob")
        _agent_file(tmp_env, "bob")
        tok = runtime.conversation_id.set(alice)
        try:
            said = await agentmsg.send_tool("bob", "when you wake up, read PLAN.md")
        finally:
            runtime.conversation_id.reset(tok)
        assert "queued for agent 'bob'" in said
        assert "Do not wait for a reply" in said
        rows = await agentmsg.claim(db, cid=bob, agent_slug="bob")
    finally:
        await db.close()
    assert [r["body"] for r in rows] == ["when you wake up, read PLAN.md"]


async def test_an_unresolvable_address_lists_who_is_live(tmp_env):
    """One call, not a conversation: a bad address answers with the roster of
    running turns instead of "not found" and a second round-trip to find out."""
    await init_db()
    db = await get_db()
    try:
        alice = await _conv(db, agent="alice")
        bob = await _conv(db, title="bob's thread", agent="bob")
    finally:
        await db.close()
    with _Envelope("op-alice", alice), _Envelope("op-bob", bob):
        tok = runtime.conversation_id.set(alice)
        try:
            said = await agentmsg.send_tool("nobody-at-all", "hello?")
        finally:
            runtime.conversation_id.reset(tok)
    assert said.startswith("error:")
    assert "bob" in said and str(bob) in said


async def test_peers_come_from_the_broker_not_the_database(tmp_env):
    """"Who can I reach" is "who is running", and only the envelope registry
    knows that. A conversation row means a thread exists, not that a turn is in
    flight — and the registry is the one thing a compromised guest cannot
    write to."""
    await init_db()
    db = await get_db()
    try:
        idle = await _conv(db, agent="idle-one")
        live = await _conv(db, agent="live-one")
        with _Envelope("op-live", live):
            peers = await agentmsg.live_peers(db)
        after = await agentmsg.live_peers(db)
    finally:
        await db.close()
    assert [p["conversation_id"] for p in peers] == [live]
    assert idle not in [p["conversation_id"] for p in peers]
    assert after == []


async def test_receiving_a_peer_message_taints_the_turn(tmp_env):
    """A peer's words are peer-authored, and that peer may have been reading the
    web. A memory_write made after receiving one is a laundering path, so the
    turn is stamped untrusted — but ONLY when something actually arrived, or
    every turn in the system would come up tainted for checking empty mail."""
    await init_db()
    db = await get_db()
    try:
        alice = await _conv(db, agent="alice")
        bob = await _conv(db, agent="bob")
    finally:
        await db.close()

    with _Envelope("op-bob", bob):
        await _gateway_call("op-bob", "inbox_fetch", {})
        assert not broker.op_tainted("op-bob"), "an empty mailbox tainted the turn"

    db = await get_db()
    try:
        await agentmsg.send(db, sender_cid=alice, to=str(bob), body="I read a web page")
    finally:
        await db.close()
    with _Envelope("op-bob", bob):
        await _gateway_call("op-bob", "inbox_fetch", {})
        assert broker.op_tainted("op-bob")


async def test_send_message_does_not_wait_for_the_recipient(tmp_env):
    """Non-blocking by construction: one INSERT and a best-effort bus nudge. A
    recipient that is wedged mid-turn cannot stall the sender."""
    await init_db()
    db = await get_db()
    try:
        alice = await _conv(db, agent="alice")
        bob = await _conv(db, agent="bob")
        with _Envelope("op-bob", bob):
            tok = runtime.conversation_id.set(alice)
            try:
                said = await asyncio.wait_for(
                    agentmsg.send_tool(str(bob), "heads up"), timeout=2)
            finally:
                runtime.conversation_id.reset(tok)
    finally:
        await db.close()
    assert said.startswith("sent to conversation")


# --- the tool is real, and reachable -----------------------------------------

def test_send_message_is_offered_and_inbox_fetch_is_not(tmp_env):
    """`inbox_fetch` exists as a tool folder only because a tool folder is what
    a guest can invoke across the vsock boundary. It must never reach a model:
    the loop already drains every round, so offering it would let the model
    spend a round asking for messages it was going to be handed anyway."""
    specs = {s["function"]["name"]
             for s in registry.openai_tool_specs(registry.load_registry())}
    assert "send_message" in specs
    assert "inbox_fetch" not in specs


def test_messaging_is_withheld_from_a_read_only_project(tmp_env):
    """A message can ask a MORE privileged agent to act. A read_only project
    must not have that lever; above read_only it is granted."""
    from backend import autonomy
    entries = [{"name": "send_message"}]
    assert autonomy.filter_entries(entries, "read_only") == []
    assert autonomy.filter_entries(entries, "stage") == entries


def test_subagents_keep_messaging(tmp_env):
    """The whole point is siblings talking, so this is NOT a delegation tool a
    worker gets stripped of."""
    from backend import autonomy
    assert "send_message" not in autonomy.NON_DELEGABLE


# --- defect (a): the run tree was disconnected where Jarvis delegates --------

def _builder(tmp_env):
    _agent_file(tmp_env, "builder")
    return {"slug": "builder", "name": "Builder", "prompt": "You build.",
            "description": "", "model": "", "base_url": "",
            "context_exclude": [], "tools_exclude": [], "skills_exclude": [],
            "max_iterations": 0}


async def test_a_spawned_agent_is_a_child_of_the_turn_that_spawned_it(
        tmp_env, monkeypatch):
    """`spawn_agent`/`spawn_temp_agent` opened their child with no parent, while
    the orchestrator and research both set one — so the run tree was broken at
    exactly the point Jarvis delegates from a chat, and a spawned agent was an
    orphan node with no way back to the conversation that asked for it.

    Driven through the broker, because that is the only path in production: the
    guest's tool call lands on the gateway's task and the parent is whatever the
    op_id's envelope says, not whatever contextvar the caller happened to hold."""
    from backend import agents_run
    from backend.memory import ensure_memory_seeds
    await init_db()
    ensure_memory_seeds()
    _builder(tmp_env)

    async def fake_turn(cid, sysp, hist, **kw):
        yield {"type": "final", "content": "built it"}
    monkeypatch.setattr(agents_run, "run_agent_turn", fake_turn)

    db = await get_db()
    try:
        chat_cid = await _conv(db, title="the operator's chat")
    finally:
        await db.close()

    with _Envelope("chat:1", chat_cid):
        out = await _gateway_call("chat:1", "spawn_agent",
                                  {"agent": "builder", "task": "do the thing"})
    assert "built it" in out["result"]

    db = await get_db()
    try:
        async with db.execute(
            "SELECT id, parent_conversation_id FROM conversations "
            "WHERE kind = 'agent'") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert len(rows) == 1
    assert rows[0]["parent_conversation_id"] == chat_cid, (
        "the spawned agent's conversation has no parent — the run tree is "
        "disconnected exactly where Jarvis delegates")


async def test_a_scheduled_run_still_has_no_parent(tmp_env, monkeypatch):
    """The parent is the turn that asked, and a schedule is nobody's child. A
    fabricated parent would be worse than none."""
    from backend import agents_run
    from backend.memory import ensure_memory_seeds
    await init_db()
    ensure_memory_seeds()

    async def fake_turn(cid, sysp, hist, **kw):
        yield {"type": "final", "content": "ok"}
    monkeypatch.setattr(agents_run, "run_agent_turn", fake_turn)
    await agents_run._run_headless(_builder(tmp_env), "nightly", active=None)

    db = await get_db()
    try:
        async with db.execute(
            "SELECT parent_conversation_id FROM conversations WHERE kind='agent'") as cur:
            assert (await cur.fetchone())["parent_conversation_id"] is None
    finally:
        await db.close()


async def test_the_chat_to_job_link_survives_a_reload(tmp_env, monkeypatch):
    """`bus.announce_job` publishes a one-off event and stores nothing, so
    reloading a chat lost the pointer to the job it launched. The job's head node
    now carries parent_conversation_id, which makes the link a query."""
    from backend import orchestrator
    from backend.db import open_conversation
    await init_db()

    db = await get_db()
    try:
        chat_cid = await open_conversation(db, project=None, title="a chat")
    finally:
        await db.close()

    async def fake_node(**kw):
        return {"cid": kw["cid"], "kind": "head", "output": "o", "rollup": "r"}
    monkeypatch.setattr(orchestrator, "run_node", fake_node)

    tok = runtime.conversation_id.set(chat_cid)
    try:
        r = await orchestrator.run_job("job-xyz", "build a thing", None,
                                       title="build a thing")
    finally:
        runtime.conversation_id.reset(tok)

    db = await get_db()
    try:
        async with db.execute(
            "SELECT id, job_id FROM conversations WHERE parent_conversation_id = ? "
            "AND job_id IS NOT NULL", (chat_cid,)) as cur:
            rows = [dict(x) for x in await cur.fetchall()]
    finally:
        await db.close()
    assert rows == [{"id": r["root_id"], "job_id": "job-xyz"}], (
        "the chat that launched this job cannot be recovered from the DB, so a "
        "reload loses the run exactly as it did before")


async def test_deleting_a_chat_that_spawned_an_agent_still_works(tmp_env):
    """The cost of connecting the run tree, paid rather than shipped.

    `get_db` sets foreign_keys=ON and two columns reference conversations. Until
    a spawned agent recorded its parent, a chat never had a child row and
    deleting one could never fail; now it does, and the DELETE would 500 with
    `FOREIGN KEY constraint failed`. Same for a message sent to or from it."""
    from backend.chat import delete_conversation
    from backend.db import open_conversation
    await init_db()
    db = await get_db()
    try:
        chat = await open_conversation(db, project=None, title="a chat")
        child = await open_conversation(db, project=None, title="[Builder] x",
                                        kind="agent", parent=chat)
        other = await open_conversation(db, project=None, title="peer")
        await db.execute(
            "INSERT INTO agent_messages (from_conversation_id, from_label, "
            "to_conversation_id, body) VALUES (?,?,?,?)", (chat, "jarvis", other, "hi"))
        await db.commit()
    finally:
        await db.close()

    assert (await delete_conversation(chat)) == {"ok": True}

    db = await get_db()
    try:
        async with db.execute(
            "SELECT parent_conversation_id FROM conversations WHERE id = ?",
            (child,)) as cur:
            assert (await cur.fetchone())["parent_conversation_id"] is None, (
                "the agent run should survive as a root, not vanish with the "
                "chat that asked for it")
        # the message it SENT survives, minus its reply address; the one sent
        # TO it is gone, because nothing could ever claim it now
        async with db.execute(
            "SELECT from_conversation_id, to_conversation_id FROM agent_messages") as cur:
            left = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert left == [{"from_conversation_id": None, "to_conversation_id": other}]


async def test_an_incognito_turn_is_not_addressable(tmp_env):
    """Its conversation is deleted in its own finally, so a message addressed to
    it would be a row pointing at nothing — and would make the wipe fail."""
    await init_db()
    db = await get_db()
    try:
        secret = await _conv(db, title="incognito")
        env = broker.TurnEnvelope(op_id="op-secret", conversation_id=secret,
                                  ephemeral=True)
        broker.register_turn(env)
        try:
            assert await agentmsg.live_peers(db) == []
        finally:
            broker.release_turn("op-secret")
    finally:
        await db.close()


async def test_an_agent_run_is_addressable_by_its_slug(tmp_env, monkeypatch):
    """Slug addressing has to reach the thing it names.

    `conversations.agent_slug` is the identity WP4 introduced, and the chat path
    sets it — but every agent RUN (spawned, scheduled, interactive) opened its
    conversation without `agent=`, so the column was NULL for exactly the turns
    an operator would name. `send_message('builder')` answered "nothing is
    running under that address", which was the opposite of the truth, and the
    queued row was never claimed because the claim matches on that same NULL."""
    from backend import agents_run
    from backend.memory import ensure_memory_seeds
    await init_db()
    ensure_memory_seeds()
    agent = _builder(tmp_env)

    async def fake_turn(cid, sysp, hist, **kw):
        yield {"type": "final", "content": "ok"}
    monkeypatch.setattr(agents_run, "run_agent_turn", fake_turn)
    run = await agents_run._run_headless(agent, "build it", active=None)
    cid = run["conversation_id"]

    db = await get_db()
    try:
        async with db.execute(
            "SELECT agent_slug FROM conversations WHERE id = ?", (cid,)) as cur:
            assert (await cur.fetchone())["agent_slug"] == "builder", (
                "an agent run's conversation carries no identity, so nothing "
                "can address it by name")

        sender = await _conv(db, title="jarvis")
        with _Envelope("guest:1", cid), _Envelope("chat:2", sender):
            peers = await agentmsg.live_peers(db, exclude_cid=sender)
            assert [p["agent"] for p in peers] == ["builder"]
            tok = runtime.conversation_id.set(sender)
            try:
                said = await agentmsg.send_tool("builder", "check the schema first")
            finally:
                runtime.conversation_id.reset(tok)
            assert said.startswith("sent to agent 'builder' — running now"), said
        rows = await agentmsg.claim(db, cid=cid, agent_slug="builder")
    finally:
        await db.close()
    assert [r["body"] for r in rows] == ["check the schema first"]


async def test_a_message_from_an_agent_run_is_labelled_with_its_slug(
        tmp_env, monkeypatch):
    """The reply address, too: an unlabelled sender reads as a generic 'agent'
    and gives the recipient nothing to answer."""
    from backend import agents_run
    from backend.memory import ensure_memory_seeds
    await init_db()
    ensure_memory_seeds()

    async def fake_turn(cid, sysp, hist, **kw):
        yield {"type": "final", "content": "ok"}
    monkeypatch.setattr(agents_run, "run_agent_turn", fake_turn)
    run = await agents_run._run_headless(_builder(tmp_env), "build it", active=None)

    db = await get_db()
    try:
        peer = await _conv(db, title="a chat")
        tok = runtime.conversation_id.set(run["conversation_id"])
        try:
            await agentmsg.send_tool(str(peer), "done, it compiles")
        finally:
            runtime.conversation_id.reset(tok)
        async with db.execute("SELECT from_label FROM agent_messages") as cur:
            assert (await cur.fetchone())["from_label"] == "builder"
    finally:
        await db.close()


async def test_a_temp_agent_run_has_no_slug_to_be_addressed_by(tmp_env, monkeypatch):
    """`spawn_temp_agent` mints an in-memory definition with no roster entry, so
    there is no slug to bind — and inventing one would name a thing that is gone
    the moment the run ends."""
    from backend import agents_run
    from backend.memory import ensure_memory_seeds
    await init_db()
    ensure_memory_seeds()

    async def fake_turn(cid, sysp, hist, **kw):
        yield {"type": "final", "content": "ok"}
    monkeypatch.setattr(agents_run, "run_agent_turn", fake_turn)
    run = await agents_run.run_temp_agent_headless("You are a worker.", "do it",
                                                   active=None)
    db = await get_db()
    try:
        async with db.execute(
            "SELECT agent_slug FROM conversations WHERE id = ?",
            (run["conversation_id"],)) as cur:
            assert (await cur.fetchone())["agent_slug"] is None
    finally:
        await db.close()


# --- incognito ---------------------------------------------------------------

async def test_an_incognito_turn_cannot_send(tmp_env):
    """Incognito's contract is that nothing survives the turn. Delivering a
    message copies its words into another agent's PERMANENT transcript, which
    breaks that outright — so the send is refused up front.

    The behaviour before this was the worst of the three options: the tool
    accepted the call, told the model the message was queued, and then the
    turn's own wipe deleted the row on the way out (`_drop_references` matched
    `from_conversation_id`). A promise, then a silent destruction."""
    await init_db()
    db = await get_db()
    try:
        secret = await _conv(db, title="incognito")
        peer = await _conv(db, title="peer", agent="bob")
    finally:
        await db.close()

    with _Envelope("chat:1", secret, ephemeral=True), _Envelope("chat:2", peer):
        out = await _gateway_call("chat:1", "send_message",
                                  {"to": str(peer), "message": "the secret is X"})
    assert out["result"].startswith("error:")
    assert "temporary chat" in out["result"]

    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) AS c FROM agent_messages") as cur:
            assert (await cur.fetchone())["c"] == 0, (
                "an incognito turn queued a message that its own wipe will "
                "delete — the recipient is promised something that cannot arrive")
    finally:
        await db.close()


async def test_an_incognito_turn_cannot_be_addressed_by_its_id_either(tmp_env):
    """`live_peers` hides it, so this needs the id named outright — but a
    guessed integer is enough, and the outcome would be the same silent
    destruction: queued, promised, then erased by the recipient's own wipe."""
    await init_db()
    db = await get_db()
    try:
        secret = await _conv(db, title="incognito")
        sender = await _conv(db, title="a chat")
        with _Envelope("chat:1", secret, ephemeral=True), _Envelope("chat:2", sender):
            tok = runtime.conversation_id.set(sender)
            try:
                said = await agentmsg.send_tool(str(secret), "hello?")
            finally:
                runtime.conversation_id.reset(tok)
        async with db.execute("SELECT COUNT(*) AS c FROM agent_messages") as cur:
            assert (await cur.fetchone())["c"] == 0
    finally:
        await db.close()
    assert "temporary chat" in said and "Nothing was sent" in said


async def test_deleting_a_chat_keeps_the_messages_it_sent(tmp_env):
    """A message is addressed to somebody else; the sender going away is not a
    reason to destroy it. `from_label` is denormalised onto the row precisely so
    it stays readable once the sender's conversation is gone. Only messages
    addressed TO the deleted conversation are removed — those can never be
    claimed by anyone."""
    from backend.chat import delete_conversation
    await init_db()
    db = await get_db()
    try:
        sender = await _conv(db, title="a chat")
        recipient = await _conv(db, title="peer", agent="bob")
        await agentmsg.send(db, sender_cid=sender, to=str(recipient),
                            body="outstanding work")
        await agentmsg.send(db, sender_cid=recipient, to=str(sender),
                            body="for the doomed chat")
    finally:
        await db.close()

    await delete_conversation(sender)

    db = await get_db()
    try:
        async with db.execute(
            "SELECT from_conversation_id, from_label, body FROM agent_messages") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        left = await agentmsg.claim(db, cid=recipient, agent_slug="bob")
    finally:
        await db.close()
    assert [r["body"] for r in rows] == ["outstanding work"]
    assert rows[0]["from_conversation_id"] is None    # FK cleared, not the row
    assert rows[0]["from_label"] == "jarvis"          # still says who sent it
    assert [r["body"] for r in left] == ["outstanding work"]


# --- defect (b): the latent resume hang -------------------------------------

async def test_job_end_is_published_only_after_the_run_leaves_active_runs(
        tmp_env, monkeypatch):
    """`resume_run_stream` subscribes and THEN checks `_active_runs`, so the flag
    has to be a promise: if a re-attach still sees it, the end signal must still
    be ahead of it in the queue. `_run_interactive`'s finally had the two
    backwards — JOB_END first, pop second — so a re-attach landing between them
    subscribed after the terminal event had already gone out to nobody and
    waited on `q.get()` forever. chat.py has documented this ordering all along;
    this path had it inverted."""
    from backend import agents_run, bus
    from backend.memory import ensure_memory_seeds
    await init_db()
    ensure_memory_seeds()

    async def fake_turn(cid, sysp, hist, **kw):
        yield {"type": "final", "content": "done"}
    monkeypatch.setattr(agents_run, "run_agent_turn", fake_turn)

    db = await get_db()
    try:
        cid = await _conv(db, kind="agent")
    finally:
        await db.close()

    seen = {}
    real_publish = bus.publish

    def publish(chan, ev):
        if ev.get("type") == "job_end":
            seen["still_flagged_running"] = cid in agents_run._active_runs
        real_publish(chan, ev)
    monkeypatch.setattr(bus, "publish", publish)

    task = asyncio.create_task(
        agents_run._run_interactive(cid, _builder(tmp_env), "go", None))
    agents_run._active_runs[cid] = task
    try:
        await asyncio.wait_for(task, timeout=10)
    finally:
        agents_run._active_runs.pop(cid, None)

    assert seen.get("still_flagged_running") is False, (
        "JOB_END went out while the run was still in _active_runs — a "
        "resume_run_stream landing in that window subscribes after the terminal "
        "event and hangs forever")


async def test_the_window_the_wrong_order_opened_really_does_hang(tmp_env):
    """What the inverted order actually cost, constructed by hand.

    This does not depend on the fix — it builds the state the old ordering could
    produce (end signal already out, run still flagged active) and shows the
    consequence: `resume_run_stream` takes the live-tail branch, subscribes to a
    channel whose terminal event has already been delivered to nobody, and never
    returns. That is the failure a panel saw as an eternal spinner. With the pop
    first, this state is unreachable — which is the whole point of the ordering."""
    from backend import agents_run, bus
    await init_db()
    cid = 987654

    class _Done:
        def done(self):
            return True
    agents_run._active_runs[cid] = _Done()
    try:
        bus.publish(agents_run._chan(cid), bus.JOB_END)   # every tail settles
        resp = await agents_run.resume_run_stream(cid)    # ...and this one attaches late

        async def drain():
            async for _ in resp.body_iterator:
                pass
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(drain(), timeout=0.5)
    finally:
        agents_run._active_runs.pop(cid, None)


async def test_reattaching_to_a_finished_run_returns_idle_not_a_hang(
        tmp_env, monkeypatch):
    """The user-visible half: come back to the board after the run finished and
    the panel settles instead of spinning."""
    from backend import agents_run
    from backend.memory import ensure_memory_seeds
    await init_db()
    ensure_memory_seeds()

    async def fake_turn(cid, sysp, hist, **kw):
        yield {"type": "final", "content": "done"}
    monkeypatch.setattr(agents_run, "run_agent_turn", fake_turn)

    db = await get_db()
    try:
        cid = await _conv(db, kind="agent")
    finally:
        await db.close()
    task = asyncio.create_task(
        agents_run._run_interactive(cid, _builder(tmp_env), "go", None))
    agents_run._active_runs[cid] = task
    await asyncio.wait_for(task, timeout=10)

    resp = await agents_run.resume_run_stream(cid)
    chunks = []

    async def drain():
        async for c in resp.body_iterator:
            chunks.append(c)
    await asyncio.wait_for(drain(), timeout=5)
    assert any("idle" in str(c) for c in chunks)
