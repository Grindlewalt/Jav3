"""Phase 1: the token budget is enforced by an explicit op_id, not an ambient
Budget object. One operation registers its Budget under an id; the id propagates
into the operation's asyncio.gather children, so every agent resolves the SAME
Budget by lookup. This is the seam Phase 3 uses to meter host-side while the loop
runs in the guest (the guest passes the id; the host owns the registry).

active_budget is retained as a fallback (a bare call / a test that sets a Budget
directly) — its behavior is covered in test_phase0_seams.py; here we exercise the
op_id path."""
import asyncio

import pytest

from backend.agent import budget as bmod
from backend.agent.budget import Budget, BudgetExceeded
from backend.agent.model import Model, ModelGateway


def _script_transport(monkeypatch, *, usage=None):
    async def fake_stream_once(self, base, key, payload):
        yield {"type": "raw", "content": "hi", "tool_calls": [], "usage": usage}
    monkeypatch.setattr(Model, "_stream_once", fake_stream_once)


async def _drain(gen):
    return [ev async for ev in gen]


def test_registry_register_get_release():
    b = Budget(max_input=1, max_output=1)
    bmod.register("op-x", b)
    try:
        assert bmod.get("op-x") is b
        assert bmod.get(None) is None
        assert bmod.get("missing") is None
    finally:
        bmod.release("op-x")
    assert bmod.get("op-x") is None
    bmod.release("op-x")            # idempotent


def test_current_resolves_active_op_id_then_fallback():
    assert bmod.current() is None                 # nothing in scope
    scoped, fallback = Budget(1, 1), Budget(1, 1)
    bmod.register("op-y", scoped)
    ftoken = bmod.active_budget.set(fallback)
    otoken = bmod.active_op_id.set("op-y")
    try:
        assert bmod.current() is scoped           # op_id wins over the fallback
        bmod.active_op_id.reset(otoken)
        otoken = bmod.active_op_id.set(None)
        assert bmod.current() is fallback         # no op_id -> legacy contextvar
    finally:
        bmod.active_op_id.reset(otoken)
        bmod.active_budget.reset(ftoken)
        bmod.release("op-y")


async def test_gateway_meters_via_explicit_op_id(monkeypatch):
    _script_transport(monkeypatch,
                      usage={"prompt_tokens": 4, "completion_tokens": 2})
    b = Budget(max_input=10_000, max_output=10_000)
    bmod.register("op-1", b)
    try:
        # no active_op_id, no active_budget — only the explicit param
        await _drain(ModelGateway(api_key="k").complete(
            [{"role": "user", "content": "x"}], op_id="op-1"))
    finally:
        bmod.release("op-1")
    assert b.input_tokens == 4 and b.output_tokens == 2


async def test_op_id_shared_across_gathered_agents(monkeypatch):
    # the invariant: register once, set the op_id, and every gathered agent meters
    # into the one registered Budget (id propagates via the contextvar).
    _script_transport(monkeypatch,
                      usage={"prompt_tokens": 7, "completion_tokens": 3})
    b = Budget(max_input=10_000, max_output=10_000)
    bmod.register("op-2", b)
    token = bmod.active_op_id.set("op-2")
    gw = ModelGateway(api_key="k")

    async def one_agent():
        await _drain(gw.complete([{"role": "user", "content": "x"}]))

    try:
        await asyncio.gather(*(one_agent() for _ in range(5)))
    finally:
        bmod.active_op_id.reset(token)
        bmod.release("op-2")
    assert b.input_tokens == 35 and b.output_tokens == 15


async def test_gateway_refuses_when_op_id_budget_spent(monkeypatch):
    called = {"n": 0}

    async def fake_stream_once(self, base, key, payload):
        called["n"] += 1
        yield {"type": "raw", "content": "hi", "tool_calls": [], "usage": None}
    monkeypatch.setattr(Model, "_stream_once", fake_stream_once)

    b = Budget(max_input=100, max_output=100, input_tokens=100)
    bmod.register("op-3", b)
    try:
        with pytest.raises(BudgetExceeded):
            await _drain(ModelGateway(api_key="k").complete(
                [{"role": "user", "content": "x"}], op_id="op-3"))
    finally:
        bmod.release("op-3")
    assert called["n"] == 0            # refused before any network I/O
