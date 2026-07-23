"""Phase 0 seam tests: lock in the behavior of the seams the VM inversion is
built on — the ModelClient/ModelGateway split, the run_turn DB-decoupling sink,
the shared-budget-across-agents invariant, and the dedup helpers. The transport
is scripted, so none of this needs a DeepSeek key.

If one of these breaks, a later phase changed a load-bearing seam: the host
nucleus (key/budget/peak/ledger) must stay above the ModelClient transport, and
one Budget must stay shared across every agent in an operation."""
import asyncio

import pytest

from backend import autonomy
from backend.agent import budget as budget_mod
from backend.agent.budget import Budget, BudgetExceeded
from backend.agent.loop import db_tool_sink
from backend.agent.model import (Model, ModelClient, ModelError, ModelGateway,
                                 PeakPricingConfirmationRequired, complete_text,
                                 model)
from backend.db import get_db, init_db, open_conversation


def _script_transport(monkeypatch, *, content="hi", tool_calls=None, usage=None):
    """Patch the transport's one HTTP method to return a scripted raw event and
    record the auth key it was handed + whether it ran at all."""
    spy = {"called": False, "key": None}

    async def fake_stream_once(self, base, key, payload):
        spy["called"] = True
        spy["key"] = key
        yield {"type": "raw", "content": content,
               "tool_calls": tool_calls or [], "usage": usage}

    monkeypatch.setattr(Model, "_stream_once", fake_stream_once)
    return spy


async def _drain(gen):
    return [ev async for ev in gen]


# --- ModelClient / ModelGateway split ----------------------------------------

async def test_transport_is_the_patchable_alias():
    # the transport class the retry tests construct + patch is ModelClient, and
    # the singleton is the gateway wrapping one.
    assert Model is ModelClient
    assert isinstance(model, ModelGateway)
    assert isinstance(model.transport, ModelClient)


async def test_gateway_injects_key(monkeypatch):
    spy = _script_transport(monkeypatch, content="ok")
    gw = ModelGateway(api_key="sk-secret")
    out = await _drain(gw.complete([{"role": "user", "content": "x"}]))
    assert spy["key"] == "sk-secret"    # the host nucleus injects the key
    assert out[-1] == {"type": "message", "content": "ok",
                       "tool_calls": [], "usage": None}


async def test_gateway_meters_budget(monkeypatch):
    _script_transport(monkeypatch,
                      usage={"prompt_tokens": 10, "completion_tokens": 5})
    b = Budget(max_input=10_000, max_output=10_000)
    token = budget_mod.active_budget.set(b)
    try:
        await _drain(ModelGateway(api_key="k").complete(
            [{"role": "user", "content": "x"}]))
    finally:
        budget_mod.active_budget.reset(token)
    assert b.input_tokens == 10 and b.output_tokens == 5


async def test_gateway_refuses_when_budget_spent(monkeypatch):
    spy = _script_transport(monkeypatch)
    b = Budget(max_input=100, max_output=100, input_tokens=100)
    token = budget_mod.active_budget.set(b)
    try:
        with pytest.raises(BudgetExceeded):
            await _drain(ModelGateway(api_key="k").complete(
                [{"role": "user", "content": "x"}]))
    finally:
        budget_mod.active_budget.reset(token)
    assert spy["called"] is False       # refused before any network I/O


async def test_gateway_peak_gate_before_network(monkeypatch):
    import backend.agent.model as model_mod
    monkeypatch.setattr(model_mod, "in_peak_window", lambda *a, **k: True)
    spy = _script_transport(monkeypatch)
    with pytest.raises(PeakPricingConfirmationRequired):
        await _drain(ModelGateway(api_key="k").complete(
            [{"role": "user", "content": "x"}], conversation_id=999))
    assert spy["called"] is False


async def test_gateway_requires_key_for_default_endpoint(monkeypatch):
    _script_transport(monkeypatch)
    with pytest.raises(ModelError):
        await _drain(ModelGateway(api_key="").complete(
            [{"role": "user", "content": "x"}]))


async def test_custom_endpoint_relaxes_key(monkeypatch):
    spy = _script_transport(monkeypatch)
    # a base_url override (ollama etc.) may need no real key -> "local"
    await _drain(ModelGateway(api_key="").complete(
        [{"role": "user", "content": "x"}], base_url="http://localhost:11434"))
    assert spy["key"] == "local"


async def test_dsml_recovery_survives_the_split(monkeypatch):
    dsml = ('<｜｜DSML｜｜invoke name="read_file">'
            '<｜｜DSML｜｜parameter name="path">README.md</｜｜DSML｜｜parameter>'
            '</｜｜DSML｜｜invoke>')
    _script_transport(monkeypatch, content=dsml, tool_calls=[])
    out = await _drain(ModelGateway(api_key="k").complete(
        [{"role": "user", "content": "x"}]))
    msg = out[-1]
    assert msg["content"] == ""         # the markup was the tool call, not prose
    assert [c["function"]["name"] for c in msg["tool_calls"]] == ["read_file"]


# --- the shared-budget invariant (what Phase 3 must preserve) ------------------

async def test_one_budget_across_gathered_agents(monkeypatch):
    # One Budget, set on the contextvar, is shared by every agent in an
    # operation because contextvars propagate into asyncio.gather children.
    # Phase 3 moves this to a gateway keyed by op_id — this pins the behavior
    # that keying must reproduce.
    _script_transport(monkeypatch,
                      usage={"prompt_tokens": 7, "completion_tokens": 3})
    b = Budget(max_input=10_000, max_output=10_000)
    token = budget_mod.active_budget.set(b)
    gw = ModelGateway(api_key="k")

    async def one_agent():
        await _drain(gw.complete([{"role": "user", "content": "x"}]))

    try:
        await asyncio.gather(*(one_agent() for _ in range(5)))
    finally:
        budget_mod.active_budget.reset(token)
    assert b.input_tokens == 35 and b.output_tokens == 15   # 5x into one budget


# --- run_turn DB-decoupling sink ---------------------------------------------

async def test_db_tool_sink_records_and_truncates(tmp_env):
    await init_db()
    db = await get_db()
    try:
        cid = await open_conversation(db, project=None, title="t")
        await db_tool_sink(db, cid)("read_file", {"path": "a.py"}, "x" * 20_000)
        async with db.execute(
            "SELECT tool, args, result FROM tool_calls WHERE conversation_id=?",
            (cid,)) as cur:
            row = await cur.fetchone()
        assert row["tool"] == "read_file"
        assert '"path"' in row["args"]
        assert row["result"] == "x" * 10_000        # stored copy is truncated
    finally:
        await db.close()


# --- dedup helpers ------------------------------------------------------------

async def test_open_conversation_defaults_and_resolution(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO projects (slug, name, path) VALUES ('proj', 'Proj', 'proj')")
        await db.commit()
        cid = await open_conversation(db, project="proj", title="hello",
                                      kind="head", job_id="job1")
        async with db.execute(
            "SELECT c.summary, c.kind, c.job_id, c.project_id, p.id AS pid "
            "FROM conversations c JOIN projects p ON p.slug='proj' "
            "WHERE c.id=?", (cid,)) as cur:
            row = await cur.fetchone()
        assert row["summary"] == "hello" and row["kind"] == "head"
        assert row["job_id"] == "job1" and row["project_id"] == row["pid"]
        # unknown slug -> project_id NULL; kind defaults to 'chat'
        cid2 = await open_conversation(db, project="nope", title="t2")
        async with db.execute(
            "SELECT project_id, kind FROM conversations WHERE id=?", (cid2,)) as cur:
            row2 = await cur.fetchone()
        assert row2["project_id"] is None and row2["kind"] == "chat"
    finally:
        await db.close()


async def test_complete_text_drains_to_string(monkeypatch):
    _script_transport(monkeypatch, content="the answer")
    monkeypatch.setattr(model, "api_key", "k")          # the singleton gateway
    monkeypatch.setattr(model.transport, "api_key", "k")
    assert await complete_text("sys", "user") == "the answer"


def test_summarize_reexports_the_one_complete_text():
    from backend import summarize
    from backend.agent.model import complete_text as canonical
    assert summarize.complete_text is canonical


def test_non_delegable_is_the_single_source(tmp_env):
    assert autonomy.NON_DELEGABLE == frozenset(
        {"spawn_agent", "deploy_agents", "create_agent", "schedule_update"})
    # the subagent tool build references it: infra tools never reach a
    # delegate; spawn_agent alone is handed back below MAX_SPAWN_DEPTH
    # (depth-capped nesting, 2026-07-23) and drops out at the cap
    from backend import runtime
    from backend.agents_run import _agent_tools
    names = {s["function"]["name"] for s in _agent_tools({})}
    assert names.isdisjoint(autonomy.NON_DELEGABLE - {"spawn_agent"})
    assert "spawn_agent" in names
    tok = runtime.spawn_depth.set(autonomy.MAX_SPAWN_DEPTH)
    try:
        at_cap = {s["function"]["name"] for s in _agent_tools({})}
    finally:
        runtime.spawn_depth.reset(tok)
    assert at_cap.isdisjoint(autonomy.NON_DELEGABLE)
