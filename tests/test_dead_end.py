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
                cid, "system", [{"role": "user", "content": "go"}],
                tools=[{"type": "function",
                        "function": {"name": "x", "parameters": {}}}],
                on_tool_call=loop_mod.db_tool_sink(db, cid)):
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


async def test_first_failure_gets_course_correct_note(tmp_env, monkeypatch):
    """A single failed/empty result draws a one-line adjustment nudge before
    any streak forms — the next call should be a correction, not a shrug."""
    await init_db()
    monkeypatch.setattr(settings, "dead_end_error_streak", 4)
    monkeypatch.setattr(settings, "dead_end_force_answer", 99)
    results = iter(["error: nope", "found it"])

    async def dispatch(name, args):
        return next(results)

    seen = []
    class Spy(_ScriptedModel):
        async def complete(self, messages, tools=None, **kw):
            seen.append([str(m.get("content")) for m in messages])
            async for ev in super().complete(messages, tools=tools, **kw):
                yield ev

    model = Spy([[("probe", '{"q": "a"}')], [("probe", '{"q": "b"}')]])
    await _run(monkeypatch, model, dispatch)
    # the nudge rides the failed result into the 2nd model call...
    assert any("ONE deliberate adjustment" in m for m in seen[1])
    # ...and the successful round draws no nudge
    assert not any("ONE deliberate adjustment" in m for m in seen[2][-1:])


async def test_plan_recheck_fires_when_todo_tool_present(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "plan_recheck_every", 2)

    async def dispatch(name, args):
        return "fine result"

    seen = []
    class Spy(_ScriptedModel):
        async def complete(self, messages, tools=None, **kw):
            seen.append([str(m.get("content")) for m in messages])
            async for ev in super().complete(messages, tools=tools, **kw):
                yield ev

    from backend.agent import loop as loop_mod
    from backend.agent.tools import registry
    model = Spy([[("probe", f'{{"q": "{i}"}}')] for i in range(4)])
    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())
    loop_mod._files_seen.clear()
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.commit()
        tools = [{"type": "function", "function": {"name": n, "parameters": {}}}
                 for n in ("probe", "todo_update")]
        async for _ in loop_mod.run_turn(cid, "system",
                                         [{"role": "user", "content": "go"}],
                                         tools=tools):
            pass
    finally:
        await db.close()
    # rounds 2 and 4 (every 2nd) end with the progress check riding the result
    assert any("progress check" in m for m in seen[2])
    assert not any("progress check" in m for m in seen[1])


async def test_plan_recheck_absent_without_todo_tool(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "plan_recheck_every", 2)

    async def dispatch(name, args):
        return "fine result"

    seen = []
    class Spy(_ScriptedModel):
        async def complete(self, messages, tools=None, **kw):
            seen.append([str(m.get("content")) for m in messages])
            async for ev in super().complete(messages, tools=tools, **kw):
                yield ev

    model = Spy([[("probe", f'{{"q": "{i}"}}')] for i in range(4)])
    await _run(monkeypatch, model, dispatch)   # tools = just "x"/probe, no todo
    assert not any("progress check" in m for round_msgs in seen
                   for m in round_msgs)


async def test_delegated_results_carry_trust_note(tmp_env, monkeypatch):
    """A successful research/spawn_agent result gets the fire-and-continue
    note; a failed one doesn't (the model should react to the error, not
    'trust' it)."""
    await init_db()

    async def dispatch(name, args):
        if args.get("q") == "bad":
            return "error: subagent died"
        return "Delegated findings: X is 42." if name in (
            "research", "spawn_agent") else "plain result"

    seen = []
    class Spy(_ScriptedModel):
        async def complete(self, messages, tools=None, **kw):
            seen.append([str(m.get("content")) for m in messages])
            async for ev in super().complete(messages, tools=tools, **kw):
                yield ev

    model = Spy([[("research", '{"q": "ok"}')],
                 [("spawn_agent", '{"q": "bad"}')],
                 [("web_read", '{"q": "ok"}')]])
    await _run(monkeypatch, model, dispatch)
    # round 1: successful research → trust note rides the result
    assert any("do NOT re-fetch" in m for m in seen[1])
    # round 2: failed spawn_agent → no trust note on that result
    assert not any("do NOT re-fetch" in m for m in seen[2][-1:])
    # round 3: ordinary tool → no trust note
    assert not any("do NOT re-fetch" in m for m in seen[3][-1:])


async def test_handrolled_web_calls_draw_research_nudge_once(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "web_handroll_nudge", 3)

    async def dispatch(name, args):
        return "page text"

    seen = []
    class Spy(_ScriptedModel):
        async def complete(self, messages, tools=None, **kw):
            seen.append([str(m.get("content")) for m in messages])
            async for ev in super().complete(messages, tools=tools, **kw):
                yield ev

    from backend.agent import loop as loop_mod
    from backend.agent.tools import registry
    model = Spy([[("web_read", f'{{"url": "https://s{i}"}}')] for i in range(5)])
    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset({"web_read"}))
    loop_mod._files_seen.clear()
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.commit()
        tools = [{"type": "function", "function": {"name": n, "parameters": {}}}
                 for n in ("web_read", "research")]
        async for _ in loop_mod.run_turn(cid, "system",
                                         [{"role": "user", "content": "go"}],
                                         tools=tools):
            pass
    finally:
        await db.close()
    # fires once when the 3rd hand-rolled call lands, never again
    joined = ["\n".join(msgs) for msgs in seen]
    assert "hand-rolled web" not in joined[2]
    assert "hand-rolled web" in joined[3]
    assert joined[4].count("hand-rolled web") == 1


async def test_no_handroll_nudge_without_research_tool(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "web_handroll_nudge", 2)

    async def dispatch(name, args):
        return "page text"

    seen = []
    class Spy(_ScriptedModel):
        async def complete(self, messages, tools=None, **kw):
            seen.append([str(m.get("content")) for m in messages])
            async for ev in super().complete(messages, tools=tools, **kw):
                yield ev

    model = Spy([[("web_read", f'{{"url": "https://s{i}"}}')] for i in range(3)])
    await _run(monkeypatch, model, dispatch,
               read_only=frozenset({"web_read"}))    # tools: just "x", no research
    assert not any("hand-rolled web" in m for msgs in seen for m in msgs)


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