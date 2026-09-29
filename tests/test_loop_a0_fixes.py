"""A0 hunt fixes in the ReAct loop (2026-09-29): tool arguments that are valid
JSON but not an object no longer end the turn, and a run that owes a
plan_report can still file it on its last round."""
from backend.db import get_db, init_db


class _Model:
    """Replays scripted rounds; records the tool names offered each call."""
    def __init__(self, rounds, last="done"):
        self.rounds, self.last = rounds, last
        self.call = 0
        self.offered = []

    async def complete(self, messages, tools=None, **kw):
        self.offered.append(sorted((t.get("function") or {}).get("name")
                                   for t in (tools or [])))
        if self.call < len(self.rounds):
            calls = [{"id": f"c{self.call}_{j}", "type": "function",
                      "function": {"name": n, "arguments": a}}
                     for j, (n, a) in enumerate(self.rounds[self.call])]
            self.call += 1
            yield {"type": "message", "content": "", "tool_calls": calls, "usage": None}
        else:
            yield {"type": "message", "content": self.last, "tool_calls": [], "usage": None}


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


async def _run(monkeypatch, model, dispatch, tools, max_iterations=None):
    from backend.agent import loop as loop_mod
    from backend.agent.tools import registry
    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())
    loop_mod._files_seen.clear()
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.commit()
        events = []
        async for ev in loop_mod.run_turn(
                cid, "system", [{"role": "user", "content": "go"}], tools=tools,
                max_iterations=max_iterations,
                on_tool_call=loop_mod.db_tool_sink(db, cid)):
            events.append(ev)
        return events
    finally:
        await db.close()


async def test_non_object_arguments_do_not_end_the_turn(tmp_env, monkeypatch):
    await init_db()
    got = []

    async def dispatch(name, args):
        got.append(args)
        return "ok"

    model = _Model([[("x", '["notes.txt"]')], [("x", "null")], [("x", '"str"')]])
    events = await _run(monkeypatch, model, dispatch, [_tool("x")])
    assert events[-1] == {"type": "final", "content": "done"}
    assert got == [{}, {}, {}]


async def test_last_round_offers_only_plan_report(tmp_env, monkeypatch):
    await init_db()
    ran = []

    async def dispatch(name, args):
        ran.append(name)
        return "ok"

    rounds = [[("x", "{}")], [("x", "{}")],
              [("plan_report", '{"status": "done", "summary": "built it"}')]]
    model = _Model(rounds)
    events = await _run(monkeypatch, model, dispatch,
                        [_tool("x"), _tool("plan_report")], max_iterations=3)
    assert model.offered[2] == ["plan_report"]
    assert ran == ["x", "x", "plan_report"]
    assert events[-1]["type"] == "final"
    assert "iteration limit" not in events[-1]["content"]


async def test_last_round_other_calls_are_not_run(tmp_env, monkeypatch):
    await init_db()
    ran = []

    async def dispatch(name, args):
        ran.append(name)
        return "ok"

    model = _Model([[("x", "{}")], [("x", "{}")], [("x", "{}")]], last="summary")
    events = await _run(monkeypatch, model, dispatch,
                        [_tool("x"), _tool("plan_report")], max_iterations=3)
    assert ran == ["x", "x"]
    assert events[-1]["type"] == "final"


async def test_without_plan_report_the_last_round_has_no_tools(tmp_env, monkeypatch):
    await init_db()

    async def dispatch(name, args):
        return "ok"

    model = _Model([[("x", "{}")], [("x", "{}")]])
    await _run(monkeypatch, model, dispatch, [_tool("x")], max_iterations=3)
    assert model.offered[2] == []
