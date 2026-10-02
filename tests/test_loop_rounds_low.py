"""The rounds-left note: a run with a round cap is told ONCE, at ~80% of it, to write
down where it is and report.

Benchmark game 2026-10-01: agents that hit their round cap with the work done never
reported (three attempts, 16M input tokens). A plan item writes its progress to
reports/notes/<item id>.md (the file the retry brief reads) and calls plan_report;
any other run is told to finish and report what it has."""
import pytest

from backend.agent import loop as loop_mod
from backend.agent.loop import _rounds_low_note, _rounds_low_round
from backend.agent.tools import registry
from backend.db import get_db, init_db

READER = {"type": "function", "function": {"name": "reader", "parameters": {}}}
REPORT = {"type": "function", "function": {"name": "plan_report", "parameters": {}}}


class _Forever:
    """Calls `reader` every round it is offered it (unique arguments, so no round is a
    duplicate), writes a plain answer once it is not, or after `stop_after` rounds."""
    def __init__(self, stop_after=None):
        self.call, self.stop_after, self.tool_texts = 0, stop_after, []

    async def complete(self, messages, tools=None, **kw):
        # what the model is shown on this call: every tool result so far
        self.tool_texts.append([m["content"] for m in messages if m["role"] == "tool"])
        offered = {t["function"]["name"] for t in (tools or [])}
        if "reader" not in offered or (self.stop_after and self.call >= self.stop_after):
            yield {"type": "message", "content": "all done", "tool_calls": [], "usage": None}
            return
        self.call += 1
        yield {"type": "message", "content": "", "usage": None, "tool_calls": [
            {"id": f"c{self.call}", "type": "function",
             "function": {"name": "reader", "arguments": f'{{"n": {self.call}}}'}}]}


async def _run(monkeypatch, n_iter, tools=(READER,), stop_after=None):
    model = _Forever(stop_after)

    async def dispatch(name, args):
        return f"result {args.get('n')}"
    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())
    loop_mod._files_seen.clear()
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.commit()
        async for _ in loop_mod.run_turn(
                cid, "system", [{"role": "user", "content": "go"}], tools=list(tools),
                max_iterations=n_iter, on_tool_call=loop_mod.db_tool_sink(db, cid)):
            pass
    finally:
        await db.close()
    return model


def _carriers(model):
    """(0-based tool result index, text) of every result that carries the note, as
    the LAST call of the run saw them."""
    return [(k, t) for k, t in enumerate(model.tool_texts[-1]) if "rounds left" in t]


def test_threshold_is_80_percent_and_never_stacks_with_the_two_thirds_note():
    assert _rounds_low_round(60) == 48
    assert _rounds_low_round(100) == 80
    assert _rounds_low_round(10) == 8
    for n in range(4, 200):
        at = _rounds_low_round(n)
        assert at is not None and (n * 2) // 3 < at <= n - 1, n    # not the 2/3 round
    # a cap too small to fit both notes and a last round gets no note
    assert [_rounds_low_round(n) for n in (1, 2, 3)] == [None, None, None]


async def test_a_run_nearing_its_cap_is_told_once(tmp_env, monkeypatch):
    await init_db()
    model = await _run(monkeypatch, 10)
    got = _carriers(model)
    assert len(got) == 1                                  # once per run
    k, text = got[0]
    assert k == 7                                         # rides the 8th round's result
    assert "2 rounds left" in text
    assert "finish and report what you have" in text      # not a plan item
    assert "reports/notes" not in text


async def test_a_plan_item_is_told_where_to_write_its_notes(tmp_env, monkeypatch):
    await init_db()
    model = await _run(monkeypatch, 10, tools=(READER, REPORT))
    got = _carriers(model)
    assert len(got) == 1
    text = got[0][1]
    assert "2 rounds left" in text
    # the exact path the retry brief reads: reports/notes/<item id>.md
    assert "reports/notes/<item id>.md" in text
    assert "call plan_report before the cap" in text


async def test_the_note_never_shares_a_result_with_the_two_thirds_note(tmp_env, monkeypatch):
    await init_db()
    model = await _run(monkeypatch, 10)
    both = [t for t in model.tool_texts[-1] if "rounds left" in t and "tool rounds used" in t]
    assert both == []
    assert any("tool rounds used" in t for t in model.tool_texts[-1])    # the old note still fires


async def test_a_run_that_finishes_early_is_not_nagged(tmp_env, monkeypatch):
    await init_db()
    model = await _run(monkeypatch, 10, stop_after=5)
    assert _carriers(model) == []


@pytest.mark.parametrize("n_iter", [2, 3])
async def test_a_tiny_cap_gets_no_note(tmp_env, monkeypatch, n_iter):
    await init_db()
    model = await _run(monkeypatch, n_iter)
    assert _carriers(model) == []


async def test_a_dead_end_that_withdrew_the_tools_gets_no_rounds_note(tmp_env, monkeypatch):
    """Tools off means nothing is left to hurry: the breaker's own note is the one."""
    await init_db()
    model = _Forever()

    async def dispatch(name, args):
        return "error: nope"
    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.commit()
        async for _ in loop_mod.run_turn(
                cid, "system", [{"role": "user", "content": "go"}], tools=[READER],
                max_iterations=30, on_tool_call=loop_mod.db_tool_sink(db, cid)):
            pass
    finally:
        await db.close()
    assert any("tools are now disabled" in t for t in model.tool_texts[-1])   # the breaker fired
    assert _carriers(model) == []


def test_note_wording():
    plan = _rounds_low_note(12, True)
    assert plan.startswith("\n\n[system note: 12 rounds left:")
    assert "reports/notes/<item id>.md" in plan and "plan_report" in plan
    other = _rounds_low_note(12, False)
    assert "12 rounds left: finish and report what you have" in other
    assert "plan_report" not in other
