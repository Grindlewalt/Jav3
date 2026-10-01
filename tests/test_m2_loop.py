"""M2 (2026-09-30): the loop's side of the model-call fixes. A reply the output
cap cut off is continued, not returned as an answer, and its last tool call never
runs (ROBUST-14); a retry event passes through (ROBUST-15); invalid-JSON tool
arguments are named to the model instead of being replaced by {} (ROBUST-16);
the rules pass is attributed to its conversation (ROBUST-23)."""
import json

from backend.agent import loop as loop_mod


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


class _Scripted:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def complete(self, messages, tools=None, **kw):
        self.calls.append({"messages": [dict(m) for m in messages], "kw": kw})
        for ev in self.replies.pop(0):
            yield ev


def _msg(content="", calls=None, **extra):
    return {"type": "message", "content": content, "tool_calls": calls or [],
            "usage": None, **extra}


def _tc(name, args, i=0):
    return {"id": f"c{i}", "type": "function",
            "function": {"name": name, "arguments": args}}


async def _turn(monkeypatch, model, dispatch=None, tools=None, **kw):
    from backend.agent.tools import registry
    monkeypatch.setattr(loop_mod, "model", model)
    if dispatch is not None:
        monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())
    loop_mod._files_seen.clear()
    return [ev async for ev in loop_mod.run_turn(
        1, "system", [{"role": "user", "content": "go"}], tools=tools or [],
        self_check=False, **kw)]


async def test_a_length_cut_answer_is_continued_not_returned_as_final(monkeypatch):
    m = _Scripted([
        [{"type": "token", "text": "Step 1 done. Step 2 is"},
         _msg("Step 1 done. Step 2 is", finish_reason="length")],
        [{"type": "token", "text": " next."}, _msg(" next.")]])
    events = await _turn(monkeypatch, m)
    assert events[-1] == {"type": "final", "content": "Step 1 done. Step 2 is next."}
    again = m.calls[1]["messages"]
    assert again[-2] == {"role": "assistant", "content": "Step 1 done. Step 2 is"}
    assert "cut off" in again[-1]["content"]


async def test_a_length_cut_tool_call_never_runs(monkeypatch):
    ran = []

    async def dispatch(name, args):
        ran.append(name)
        return "ok"

    m = _Scripted([
        [_msg("", [_tc("write_file", '{"path": "a", "content": "hal')],
              finish_reason="length")],
        [_msg("done")]])
    events = await _turn(monkeypatch, m, dispatch, tools=[_tool("write_file")])
    assert ran == [] and events[-1]["content"] == "done"
    assert "output limit" in m.calls[1]["messages"][-1]["content"]


async def test_a_round_that_keeps_getting_cut_ends_with_what_there_is(monkeypatch):
    m = _Scripted([[_msg("part %d " % i, finish_reason="length")] for i in range(5)])
    events = await _turn(monkeypatch, m)
    final = events[-1]
    assert final["type"] == "final" and final["content"].startswith("part 0 part 1 ")
    assert "cut off" in final["content"]
    assert len(m.calls) <= 4                        # bounded: it does not loop forever


async def test_retry_event_passes_through_the_loop(monkeypatch):
    m = _Scripted([[{"type": "token", "text": "a"}, {"type": "retry", "reason": "x"},
                    {"type": "token", "text": "ab"}, _msg("ab")]])
    events = await _turn(monkeypatch, m)
    assert [e["type"] for e in events if e["type"] != "turn_stats"] == [
        "token", "retry", "token", "final"]
    assert events[-1]["content"] == "ab"


async def test_invalid_json_arguments_are_named_not_replaced_by_empty(monkeypatch):
    got = []

    async def dispatch(name, args):
        got.append((name, args))
        return "ok"

    bad = '{"path": "a.txt", "content": "she said "hi" and left"}'
    m = _Scripted([[_msg("", [_tc("write_file", bad)])], [_msg("done")]])
    events = await _turn(monkeypatch, m, dispatch, tools=[_tool("write_file")])
    assert got == []                                 # never dispatched with {}
    res = next(e for e in events if e["type"] == "tool_result")
    assert res["ok"] is False and res["result"].startswith("error:")
    assert "not valid JSON" in res["result"]
    assert '{"path": "a.txt", "content": "she said' in res["result"]   # the start of it
    assert "write_file" in res["result"]
    tool_ev = next(e for e in events if e["type"] == "tool")
    assert "she said" in json.dumps(tool_ev["args"])   # the raw text is kept, truncated


async def test_valid_but_non_object_json_still_dispatches_an_empty_call(monkeypatch):
    got = []

    async def dispatch(name, args):
        got.append(args)
        return "ok"

    m = _Scripted([[_msg("", [_tc("write_file", "[1, 2]")])], [_msg("done")]])
    await _turn(monkeypatch, m, dispatch, tools=[_tool("write_file")])
    assert got == [{}]                               # the 6dde46a behaviour




# --- ROBUST-23 ---

async def test_the_rules_pass_is_attributed_to_its_conversation(monkeypatch):
    m = _Scripted([[_msg("fixed")]])
    monkeypatch.setattr(loop_mod, "model", m)
    out = await loop_mod._enforce_rules("text", "rules", conversation_id=7)
    assert out == "fixed" and m.calls[0]["kw"]["conversation_id"] == 7


async def test_a_cut_off_rewrite_does_not_replace_the_answer(monkeypatch):
    m = _Scripted([[_msg("half of the rew", finish_reason="length")]])
    monkeypatch.setattr(loop_mod, "model", m)
    assert await loop_mod._enforce_rules("the full answer", "rules") == "the full answer"
