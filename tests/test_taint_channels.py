"""Channels that carry untrusted text into a turn must reach the broker's taint
ledger (A0 hunt MEM-06..09): a child agent's result, a quarantined note read
back, MCP (projector) results, network bytes through the egress proxy."""
import pytest

from backend import memory
from backend.vm import broker


def _reg(op_id="op-tc", **kw):
    broker.register_turn(broker.TurnEnvelope(op_id=op_id, web_session="ws", **kw))


@pytest.fixture
def fake_dispatch(monkeypatch):
    async def fake(name, args):
        return f"{name}-ok"
    monkeypatch.setattr(broker.registry, "dispatch", fake)


# --- MEM-08: projector (MCP) results ------------------------------------------

@pytest.mark.parametrize("tool", ["projector_status", "projector_show",
                                  "projector_output", "projector_universe"])
async def test_projector_result_taints_the_turn(tmp_env, fake_dispatch, tool):
    _reg()
    try:
        assert broker.classify_taint(tool) == "untrusted"
        assert broker.op_tainted("op-tc") is False
        out = await broker.broker_dispatch("op-tc", tool, {})
        assert out["taint"] == "untrusted"
        assert broker.op_tainted("op-tc") is True
    finally:
        broker.release_turn("op-tc")


@pytest.mark.parametrize("stamp", ["untrusted", "mcp:projector", "Untrusted", "yes", True])
def test_any_taint_stamp_reads_as_untrusted(stamp):
    meta = {"source": "agent", "approved": True, "taint": stamp}
    assert memory.note_taint(meta) == "untrusted"
    assert memory.note_trusted(meta) is False


@pytest.mark.parametrize("stamp", [None, "", False])
def test_empty_taint_stamp_is_clean(stamp):
    assert memory.note_taint({"taint": stamp}) == "trusted"
