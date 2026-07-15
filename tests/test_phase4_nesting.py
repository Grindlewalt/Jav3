"""Phase 4 (M4): the seams that let nested/overlapping turns share one guest.

Offline proofs of the three invariants the nested-in-guest design rests on:
- the guest's per-turn state is TASK-LOCAL, so two turns running at once in one
  guest never overwrite each other's op_id / tool specs (the module-global version
  would have the last turn to start clobber every other's op_id);
- the broker restores the operation's op_id, so a brokered spawn_agent resolves the
  PARENT operation's Budget (and knows it is nested);
- one Budget object aliased under several op_ids meters as one — releasing a child
  alias leaves the shared object alive under the parent.
The full nested guest turn (a guest chat that spawns an agent) is verified live.
"""
import io
import os
import subprocess
import sys
import tarfile
import tempfile

from backend.vm.guest_pkg import build_package_tar


def _extract_pkg():
    d = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(build_package_tar()), mode="r:gz") as t:
        t.extractall(d, filter="data")
    return d


def test_guest_turn_state_is_task_local():
    """Two concurrent turns bind different op_id/specs/rules/slug via turnctx and
    read back their OWN values throughout — the fix that makes one guest safe for a
    nested spawn (parent suspended while child runs) and overlapping chats."""
    d = _extract_pkg()
    script = (
        "import asyncio\n"
        "from backend import turnctx\n"
        "from backend.agent import model\n"
        "from backend.agent.tools import registry, toolctx\n"
        "from backend.memory import standing_rules_tail\n"
        "async def turn(op, slug, specs, rules, hops):\n"
        "    tok = turnctx.enter({'op_id': op, 'tool_specs': specs, 'rules': rules,\n"
        "                         'gateway_port': 5555, 'read_only': []}, slug)\n"
        "    try:\n"
        "        for _ in range(hops):\n"
        "            await asyncio.sleep(0)\n"  # yield so the other turn interleaves
        "            assert turnctx.op_id.get() == op, turnctx.op_id.get()\n"
        "            assert registry.openai_tool_specs() == specs\n"
        "            assert standing_rules_tail() == rules\n"
        "            assert await toolctx.active_slug() == slug\n"
        "    finally:\n"
        "        turnctx.reset(tok)\n"
        "    return op\n"
        "async def main():\n"
        "    res = await asyncio.gather(\n"
        "        turn('chat:5', 'projA', [{'a': 1}], 'RULES-A', 8),\n"
        "        turn('guest:9', 'projB', [{'b': 2}], '', 8))\n"
        "    print('OUT:' + ','.join(res))\n"
        "asyncio.run(main())\n")
    r = subprocess.run([sys.executable, "-S", "-c", script], cwd=d,
                       env={"PYTHONPATH": d, "PATH": os.environ.get("PATH", "")},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OUT:chat:5,guest:9" in r.stdout, r.stdout + r.stderr


async def test_broker_restores_operation_op_id(monkeypatch):
    """A brokered tool runs with the operation's op_id in scope, so a spawn_agent
    it invokes resolves the parent Budget and detects it is nested."""
    from backend.agent import budget as bmod
    from backend.agent.tools import registry
    from backend.vm import broker

    seen = {}

    async def fake_dispatch(name, args):
        seen["op_id"] = bmod.active_op_id.get()
        seen["nested"] = bmod.current() is not None
        return "ok"
    monkeypatch.setattr(registry, "dispatch", fake_dispatch)

    bmod.register("op-parent", bmod.Budget(10**9, 10**9))
    broker.register_turn(broker.TurnEnvelope(op_id="op-parent", web_session="ws"))
    try:
        out = await broker.broker_dispatch("op-parent", "spawn_agent", {"agent": "x"})
    finally:
        broker.release_turn("op-parent")
        bmod.release("op-parent")
    assert out["result"] == "ok"
    assert seen["op_id"] == "op-parent"        # op_id propagated into the tool
    assert seen["nested"] is True              # parent Budget resolvable -> nested
    # and it is torn back down after the call (no leak into the next turn)
    assert bmod.active_op_id.get() is None


def test_budget_object_aliased_across_op_ids_meters_as_one():
    """Nesting shares metering by registering the SAME Budget object under a fresh
    child op_id; releasing the child alias leaves the object live under the parent."""
    from backend.agent import budget as bmod
    b = bmod.Budget(10**9, 10**9)
    bmod.register("op-parent", b)
    bmod.register("op-child", b)               # alias — same object, second id
    try:
        bmod.get("op-child").add({"prompt_tokens": 100, "completion_tokens": 40})
        assert bmod.get("op-parent").input_tokens == 100     # shared metering
        assert bmod.get("op-parent").output_tokens == 40
        bmod.release("op-child")               # child turn ends
        assert bmod.get("op-child") is None
        assert bmod.get("op-parent") is b      # parent's object survives
    finally:
        bmod.release("op-parent")
