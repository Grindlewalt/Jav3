"""Dead-end circuit-breaker: duplicate read-only calls short-circuit, error
streaks get a corrective note, and a hard streak withdraws tools so the turn
concludes instead of grinding to the iteration cap (the convo-12 post-mortem)."""
from backend.config import settings
from backend.db import get_db, init_db


class _ScriptedModel:
    """Emits the given tool-call rounds, then a final answer. Records the
    tools= argument of every call so tests can see tools being withdrawn."""
    def __init__(self, rounds):
        self.rounds = rounds
        self.call = 0
        self.tools_seen = []

    async def complete(self, messages, tools=None, **kw):
        self.tools_seen.append(tools)
        if self.call < len(self.rounds):
            calls = [{"id": f"c{self.call}_{j}", "type": "function",
                      "function": {"name": name, "arguments": args}}
                     for j, (name, args) in enumerate(self.rounds[self.call])]
            self.call += 1
            yield {"type": "message", "content": "", "tool_calls": calls,
                   "usage": None}
        else:
            yield {"type": "message", "content": "concluding", "tool_calls": [],
                   "usage": None}


async def _run(monkeypatch, model, dispatch, read_only=frozenset()):
    from backend.agent import loop as loop_mod
    from backend.agent.tools import registry
    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: read_only)
    loop_mod._files_seen.clear()
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.commit()
        events = []
        async for ev in loop_mod.run_turn(
                db, cid, "system", [{"role": "user", "content": "go"}],
                tools=[{"type": "function",
                        "function": {"name": "x", "parameters": {}}}]):
            events.append(ev)
        return events, cid
    finally:
        await db.close()


async def test_duplicate_read_only_call_short_circuits(tmp_env, monkeypatch):
    await init_db()
    dispatched = []

    async def dispatch(name, args):
        dispatched.append((name, args))
        return "the page text"

    model = _ScriptedModel([
        [("web_read", '{"url": "https://a"}')],
        [("web_read", '{"url": "https://a"}')],     # exact repeat
        [("web_read", '{"url": "https://b"}')],     # different args — allowed
    ])
    _, cid = await _run(monkeypatch, model, dispatch,
                        read_only=frozenset({"web_read"}))
    assert len(dispatched) == 2                     # repeat never dispatched
    db = await get_db()
    try:
        async with db.execute(
            "SELECT result FROM tool_calls WHERE conversation_id = ? ORDER BY id",
            (cid,)) as cur:
            rows = [r["result"] for r in await cur.fetchall()]
    finally:
        await db.close()
    assert rows[1].startswith("duplicate call:")
    assert "result is unchanged" in rows[1]


async def test_duplicate_allowed_after_mutating_call(tmp_env, monkeypatch):
    await init_db()
    dispatched = []

    async def dispatch(name, args):
        dispatched.append(name)
        return "ok content"

    model = _ScriptedModel([
        [("read_file", '{"path": "a.py"}')],
        [("edit_file", '{"path": "a.py", "find": "x", "replace": "y"}')],  # mutates
        [("read_file", '{"path": "a.py"}')],        # re-read must re-dispatch
    ])
    await _run(monkeypatch, model, dispatch, read_only=frozenset({"read_file"}))
    assert dispatched == ["read_file", "edit_file", "read_file"]


async def test_error_streak_injects_corrective_note(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "dead_end_error_streak", 3)
    monkeypatch.setattr(settings, "dead_end_force_answer", 99)

    async def dispatch(name, args):
        return "error: nothing here"

    seen = []
    class Spy(_ScriptedModel):
        async def complete(self, messages, tools=None, **kw):
            seen.append([str(m.get("content")) for m in messages])
            async for ev in super().complete(messages, tools=tools, **kw):
                yield ev

    model = Spy([[("probe", f'{{"q": "{i}"}}')] for i in range(4)])
    await _run(monkeypatch, model, dispatch)
    # by the 4th model call the streak note rides the 3rd failed result
    assert any("Diagnose why" in m for m in seen[3])
    # and success resets: not asserted here — covered by the force test below


async def test_hard_streak_withdraws_tools_and_concludes(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "dead_end_error_streak", 2)
    monkeypatch.setattr(settings, "dead_end_force_answer", 3)

    async def dispatch(name, args):
        return "error: still nothing"

    model = _ScriptedModel([[("probe", f'{{"q": "{i}"}}')] for i in range(10)])
    events, _ = await _run(monkeypatch, model, dispatch)
    # after 3 consecutive failures the next model call has no tools, so the
    # scripted model still returns tool calls only while tools were granted
    assert model.tools_seen[3] is None              # tools withdrawn
    assert events[-1]["type"] == "final"
    # the loop concluded well before the 10 scripted rounds ran out
    assert model.call < 10


class _StubbornThenAnswers(_ScriptedModel):
    """Returns tool calls even when tools are withdrawn — except the very
    last (conclusion) call, which answers in text like the real model does
    when handed the tool-budget-exhausted nudge."""
    async def complete(self, messages, tools=None, **kw):
        if tools is None and "tool budget for this turn is exhausted" in \
                str(messages[-1].get("content", "")):
            self.tools_seen.append(tools)
            yield {"type": "message",
                   "content": "Here is what I found; X remains unknown.",
                   "tool_calls": [], "usage": None}
            return
        async for ev in super().complete(messages, tools=tools, **kw):
            yield ev


async def test_exhaustion_forces_text_conclusion_not_bare_stop(tmp_env, monkeypatch):
    """Convo-31 regression: tools withdrawn, model emits tool calls anyway
    (DSML recovery) — those calls must NOT execute, and the turn must end
    with a real answer synthesized from the transcript."""
    await init_db()
    monkeypatch.setattr(settings, "dead_end_error_streak", 2)
    monkeypatch.setattr(settings, "dead_end_force_answer", 3)
    dispatched = []

    async def dispatch(name, args):
        dispatched.append(name)
        return "error: nope"

    model = _StubbornThenAnswers([[("probe", f'{{"q": "{i}"}}')] for i in range(10)])
    events, _ = await _run(monkeypatch, model, dispatch)
    assert events[-1] == {"type": "final",
                          "content": "Here is what I found; X remains unknown."}
    # exactly the 3 pre-breaker rounds dispatched; the post-withdrawal tool
    # calls were dropped, not executed
    assert len(dispatched) == 3


async def test_iteration_cap_forces_text_conclusion(tmp_env, monkeypatch):
    """The plain cap-exhaustion path (no breaker): the final round has no
    tools; if calls come back anyway the loop must still produce an answer."""
    await init_db()
    monkeypatch.setattr(settings, "max_react_iterations", 3)

    async def dispatch(name, args):
        return "useful finding"

    model = _StubbornThenAnswers(
        [[("probe", f'{{"q": "{i}"}}')] for i in range(10)])
    events, _ = await _run(monkeypatch, model, dispatch)
    assert events[-1]["content"] == "Here is what I found; X remains unknown."


async def test_tool_events_carry_id_and_results_stream(tmp_env, monkeypatch):
    """F1: every tool event has an id, and a matching tool_result event
    follows execution with ok/err classification + the result payload."""
    await init_db()

    async def dispatch(name, args):
        return "error: nope" if args.get("q") == "bad" else "found: 42"

    model = _ScriptedModel([[("probe", '{"q": "good"}'), ("probe", '{"q": "bad"}')]])
    events, _ = await _run(monkeypatch, model, dispatch)
    tools = [e for e in events if e["type"] == "tool"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(tools) == 2 and len(results) == 2
    assert all(e.get("id") for e in tools)
    by_id = {e["id"]: e for e in results}
    assert by_id[tools[0]["id"]]["ok"] is True
    assert by_id[tools[0]["id"]]["result"] == "found: 42"
    assert by_id[tools[1]["id"]]["ok"] is False


async def test_success_resets_streak(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "dead_end_error_streak", 3)
    monkeypatch.setattr(settings, "dead_end_force_answer", 4)
    results = iter(["error: a", "error: b", "found it!", "error: c",
                    "error: d", "error: e"])

    async def dispatch(name, args):
        return next(results)

    seen = []
    class Spy(_ScriptedModel):
        async def complete(self, messages, tools=None, **kw):
            seen.append([str(m.get("content")) for m in messages])
            async for ev in super().complete(messages, tools=tools, **kw):
                yield ev

    model = Spy([[("probe", f'{{"q": "{i}"}}')] for i in range(6)])
    await _run(monkeypatch, model, dispatch)
    # the success at call 3 reset the streak: calls 4-5 are failures 1-2,
    # so no note exists after round 4 (streak 2 < 3)...
    assert not any("Diagnose why" in m for m in seen[5])
    # ...and the note appears once the fresh streak reaches 3 at round 6
    assert any("Diagnose why" in m for m in seen[6])