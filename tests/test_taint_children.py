"""MEM-06: taint flows from a child agent up to the turn that delegated to it.
Delegation is the prompt's default for volume work; without this a parent could
launder a child's web-derived report into memory with clean provenance."""
import pytest

from backend import memory
from backend.agent import budget as budget_mod
from backend.vm import broker, guest_turn as guest_turn_mod
from backend.vm import turn as turn_mod


@pytest.fixture
def fake_dispatch(monkeypatch):
    async def fake(name, args):
        return f"{name}-ok"
    monkeypatch.setattr(broker.registry, "dispatch", fake)


def _reg(op, parent=None):
    broker.register_turn(broker.TurnEnvelope(op_id=op, web_session="ws", parent_op=parent))


async def test_a_tainted_child_taints_its_parent_when_it_ends(tmp_env, fake_dispatch):
    _reg("parent")
    _reg("child", parent="parent")
    try:
        await broker.broker_dispatch("child", "web_read", {"url": "http://x"})
        assert broker.op_tainted("child") and not broker.op_tainted("parent")
        broker.release_turn("child")
        assert broker.op_tainted("parent") is True
    finally:
        broker.release_turn("child")
        broker.release_turn("parent")


async def test_a_clean_child_leaves_its_parent_clean(tmp_env, fake_dispatch):
    _reg("parent")
    _reg("child", parent="parent")
    try:
        await broker.broker_dispatch("child", "read_file", {"path": "a"})
        broker.release_turn("child")
        assert broker.op_tainted("parent") is False
    finally:
        broker.release_turn("parent")


async def test_taint_climbs_more_than_one_level(tmp_env, fake_dispatch):
    _reg("root")
    _reg("mid", parent="root")
    _reg("leaf", parent="mid")
    try:
        await broker.broker_dispatch("leaf", "web_search", {"query": "q"})
        broker.release_turn("leaf")
        assert broker.op_tainted("mid") and not broker.op_tainted("root")
        broker.release_turn("mid")
        assert broker.op_tainted("root") is True
    finally:
        for op in ("leaf", "mid", "root"):
            broker.release_turn(op)


async def test_the_screen_or_page_source_travels_too(tmp_env, fake_dispatch):
    _reg("parent")
    _reg("child", parent="parent")
    try:
        await broker.broker_dispatch("child", "browser_read_page", {})
        broker.release_turn("child")
        assert broker._nav_tainted.get("parent") == "browser"
    finally:
        broker.release_turn("parent")


async def test_a_parent_that_already_finished_gets_no_stale_entry(tmp_env, fake_dispatch):
    _reg("parent")
    _reg("child", parent="parent")
    broker.release_turn("parent")            # fire-and-forget child outlives it
    try:
        await broker.broker_dispatch("child", "web_read", {"url": "http://x"})
    finally:
        broker.release_turn("child")
    assert "parent" not in broker._tainted


async def test_the_delegating_call_result_says_the_report_is_derived(tmp_env, monkeypatch):
    """spawn_agent returns the child's report to the parent: the parent's next
    memory_write is stamped, and the result it just got says why."""
    real = broker.registry.dispatch

    async def fake(name, args):
        if name == "spawn_agent":
            _reg("child", parent="parent")
            await broker.broker_dispatch("child", "web_read", {"url": "http://x"})
            broker.release_turn("child")
            return "[a reports] done"
        if name == "web_read":
            return "a page"
        return await real(name, args)          # memory_write is the real handler
    monkeypatch.setattr(broker.registry, "dispatch", fake)
    _reg("parent")
    try:
        out = await broker.broker_dispatch("parent", "spawn_agent", {"agent": "a", "task": "t"})
        assert "[a reports] done" in out["result"]
        assert "untrusted content" in out["result"] and "quarantined" in out["result"]
        w = await broker.broker_dispatch("parent", "memory_write",
                                         {"name": "f", "content": "the child said so"})
        assert "quarantined" in w["result"]
        meta, _ = memory.parse_note((memory.notes_dir() / "f.md").read_text())
        assert memory.note_taint(meta) == "untrusted"
    finally:
        broker.release_turn("child")
        broker.release_turn("parent")


async def test_run_agent_turn_links_a_nested_turn_to_its_parent(tmp_env, monkeypatch):
    seen = []

    async def fake_guest_turn(conversation_id, system_prompt, history, **kw):
        seen.append(kw["envelope"])
        yield {"type": "final", "content": "done"}

    monkeypatch.setattr(guest_turn_mod, "guest_turn", fake_guest_turn)

    async def run(cid):
        return [ev async for ev in turn_mod.run_agent_turn(cid, "sys", [], tools=[], read_only=[],
                                                           inbox=False)]

    # top level: nothing in scope
    await run(1)
    assert seen[-1].parent_op is None
    # nested: inside a brokered spawn (an operation's Budget and op id are in scope)
    budget_mod.register("chat:9", budget_mod.Budget(10_000, 10_000))
    tok = budget_mod.active_op_id.set("chat:9")
    try:
        await run(2)
    finally:
        budget_mod.active_op_id.reset(tok)
        budget_mod.release("chat:9")
    assert seen[-1].parent_op == "chat:9"
