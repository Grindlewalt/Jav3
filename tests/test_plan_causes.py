"""Plan item failures say how they really ended, and a retry is told where the
last attempt stopped (benchmark-game run, conv 500: 40 of 40 failed attempts
read "no structured completion report" whatever had happened).

Same offline harness as test_plan.py: an item's turn is a scripted stand-in for
`agents_run.run_agent_turn`; here it also emits tool events and a loop stop."""
from backend import agents_run, turnstats
from backend import plan as plan_mod
from backend.config import settings
from tests.test_plan import (SLUG, _by_id, _put, _wait_run, client)  # noqa: F401

CAP_FINAL = "(stopped: hit the ReAct iteration limit without finishing)"


async def _no_synth(system, user, temperature=0.3):
    return "ROLLUP"


def _loop(script: dict, seen: dict):
    """script: item id -> async fn(cid, attempt, text) -> (calls, final, stop).
    calls = [(tool name, args, result)], emitted as the loop emits them; `stop`
    rides the final event only for the cases the real loop marks there."""
    async def turn(cid, system_prompt, history, **kw):
        info = plan_mod.live_item(cid)
        assert info is not None
        seen.setdefault(info["item_id"], []).append(history[0]["content"])
        calls, final, stop = await script[info["item_id"]](
            cid, len(seen[info["item_id"]]), history[0]["content"])
        for n, (name, args, result) in enumerate(calls):
            yield {"type": "tool", "id": f"c{n}", "name": name, "args": args}
            yield {"type": "tool_result", "id": f"c{n}", "name": name, "result": result}
        yield {"type": "final", "content": final, **({"stop": stop} if stop else {})}
    return turn


CALLS = [("read_file", {"path": "backend/a.py"}, "ok"),
         ("read_file", {"path": "backend/c.py"}, "ok"),
         ("write_file", {"path": "backend/b.py", "content": "x"}, "ok"),
         ("run_code", {"command": "pytest -q tests/test_b.py"}, "error: 2 failed")]


async def _run(client, monkeypatch, script, seen, **plan):
    monkeypatch.setattr(agents_run, "run_agent_turn", _loop(script, seen))
    monkeypatch.setattr(plan_mod, "complete_text", _no_synth)
    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    return plan_mod.load(SLUG)


def _section(text: str) -> str:
    """The retry brief's added section."""
    head = "# Continue from where the last attempt stopped"
    assert head in text
    return text[text.index(head):].split("\n# Budget")[0]


# --- the cause --------------------------------------------------------------

async def test_a_round_cap_stop_is_named_continues_free_once_and_the_retry_is_briefed(
        client, tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "plan_item_max_iterations", 7)
    await _put(client, [{"title": "big", "brief": "build it"}], attempts_max=2, max_concurrent=1)
    seen: dict = {}

    async def capped(cid, attempt, text):
        if attempt == 1:                      # the loop's nudge asks for this file
            note = tmp_env / "projects" / SLUG / "reports" / "notes" / "i1.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("models done; tests in tests/test_b.py still fail on case 2")
        return CALLS, CAP_FINAL, "cap"

    p = await _run(client, monkeypatch, {"i1": capped}, seen)
    i1 = _by_id(p)["i1"]
    # run 1 is free, run 2 spends an attempt, run 3 spends the last one
    assert len(seen["i1"]) == 3
    assert i1["status"] == "failed" and i1["attempts"] == 2 and i1["continues"] == 1
    assert i1["last_error"] == "ran out of rounds (7) before calling plan_report"
    assert "plan_report was not called" not in i1["last_error"]
    h = i1["history"]
    assert [x["outcome"] for x in h] == ["cap", "cap", "cap"]
    assert "ran out of rounds (7)" in h[0]["error"] and "no attempt is spent" in h[0]["error"]
    assert "no attempt is spent" not in h[1]["error"]
    assert h[0]["attempt"] == 1 and h[1]["attempt"] == 1   # the free one did not count

    assert "Continue from where" not in seen["i1"][0]
    sec = _section(seen["i1"][1])
    assert "Attempt 1: ran out of rounds (7)" in sec
    assert "Continue from where it stopped" in sec and "call plan_report" in sec
    assert "tests/test_b.py still fail on case 2" in sec          # the handoff note
    assert "reports/notes/i1.md" in sec
    assert "- read_file backend/a.py" in sec
    assert "- run_code pytest -q tests/test_b.py (error)" in sec  # a failed call is marked
    assert "## Files it read\nbackend/a.py, backend/c.py" in sec
    assert "## Files it wrote\nbackend/b.py" in sec
    assert len(sec) <= plan_mod.RETRY_BRIEF_CHARS

    # the cause reaches what the orchestrator and the rollup read
    text = await plan_mod.status(SLUG)
    assert "error: ran out of rounds (7) before calling plan_report" in text
    detail = await plan_mod.status(SLUG, item="i1")
    assert "round-cap continuations 1/1" in detail
    assert "run_code pytest -q tests/test_b.py (error)" in detail and "wrote: backend/b.py" in detail
    assert "ran out of rounds (7)" in plan_mod._listing(p)

    async def boom(system, user, temperature=0.3):
        raise RuntimeError("no model")
    monkeypatch.setattr(plan_mod, "complete_text", boom)
    assert "ran out of rounds (7)" in await plan_mod._synthesize(SLUG, "failed")


async def test_the_free_continuation_finishes_the_item_without_spending_an_attempt(
        client, tmp_env, monkeypatch):
    await _put(client, [{"title": "big", "brief": "b"}], attempts_max=1, max_concurrent=1)
    seen: dict = {}

    async def go(cid, attempt, text):
        if attempt == 1:
            return CALLS, CAP_FINAL, "cap"
        await plan_mod.report(SLUG, cid=cid, item_id=None, status="done", summary="finished")
        return [], "done", None

    p = await _run(client, monkeypatch, {"i1": go}, seen)
    i1 = _by_id(p)["i1"]
    assert len(seen["i1"]) == 2                  # attempts_max=1 would have ended it at 1
    assert i1["status"] == "done" and i1["attempts"] == 1 and i1["continues"] == 1
    assert i1["trail"] is None and i1["last_error"] is None


async def test_a_dead_end_says_the_tools_were_withdrawn_and_spends_the_attempt(
        client, tmp_env, monkeypatch):
    await _put(client, [{"title": "stuck", "brief": "b"}], attempts_max=2, max_concurrent=1)
    seen: dict = {}

    async def stuck(cid, attempt, text):
        return CALLS, "I could not get the tests to run.", "dead_end"

    p = await _run(client, monkeypatch, {"i1": stuck}, seen)
    i1 = _by_id(p)["i1"]
    assert len(seen["i1"]) == 2 and i1["continues"] == 0 and i1["attempts"] == 2
    assert i1["status"] == "failed"
    assert i1["last_error"] == (
        f"its tools were withdrawn after {settings.dead_end_force_answer} failed or empty "
        "tool calls in a row (last call: run_code pytest -q tests/test_b.py), "
        "so it could not call plan_report")
    assert [x["outcome"] for x in i1["history"]] == ["dead_end", "dead_end"]
    assert "its tools were withdrawn" in _section(seen["i1"][1])


async def test_a_prose_final_is_still_no_report_and_spends_the_attempt(
        client, tmp_env, monkeypatch):
    await _put(client, [{"title": "chatty", "brief": "b"}], attempts_max=2, max_concurrent=1)
    seen: dict = {}

    async def chatty(cid, attempt, text):
        return [], "All done, I think.", None

    p = await _run(client, monkeypatch, {"i1": chatty}, seen)
    i1 = _by_id(p)["i1"]
    assert len(seen["i1"]) == 2 and i1["attempts"] == 2 and i1["continues"] == 0
    assert i1["last_error"] == "no structured completion report (plan_report was not called)"
    assert i1["history"][-1]["outcome"] == "failed"


async def test_a_report_filed_on_the_last_round_beats_the_cap_stop(client, tmp_env, monkeypatch):
    await _put(client, [{"title": "late", "brief": "b"}], attempts_max=2, max_concurrent=1)
    seen: dict = {}

    async def late(cid, attempt, text):
        await plan_mod.report(SLUG, cid=cid, item_id=None, status="done", summary="in time")
        return [], "(filed the plan report at the round limit)", "cap"

    p = await _run(client, monkeypatch, {"i1": late}, seen)
    i1 = _by_id(p)["i1"]
    assert len(seen["i1"]) == 1 and i1["status"] == "done" and i1["continues"] == 0


async def test_the_loops_stop_is_read_from_its_turn_stats_row_or_its_cap_sentence(
        client, tmp_env, monkeypatch):
    """guest_turn records the loop's stop in turn_stats and does not pass it on,
    and the final event names only a budget stop: that is where a real run's
    cap shows up."""
    await _put(client, [{"title": "row", "brief": "b"}, {"title": "text", "brief": "b"}],
               attempts_max=1, max_concurrent=1)
    seen: dict = {}

    async def row(cid, attempt, text):
        if attempt == 1:
            await turnstats.record(cid, f"guest:{cid}", {"stop": "cap", "rounds": 7})
            return [], "(ran out)", None
        await plan_mod.report(SLUG, cid=cid, item_id=None, status="done", summary="ok")
        return [], "done", None

    async def text_only(cid, attempt, text):
        if attempt == 2:
            await plan_mod.report(SLUG, cid=cid, item_id=None, status="done", summary="ok")
        return [], CAP_FINAL if attempt == 1 else "done", None

    p = await _run(client, monkeypatch, {"i1": row, "i2": text_only}, seen)
    its = _by_id(p)
    for iid in ("i1", "i2"):
        assert len(seen[iid]) == 2, iid          # one free continuation each
        assert its[iid]["status"] == "done" and its[iid]["continues"] == 1
        assert its[iid]["history"][0]["outcome"] == "cap"


async def test_a_budget_stop_is_still_a_budget_stop(client, tmp_env, monkeypatch):
    """Unchanged: the token budget hands the attempt back and pauses the run."""
    await _put(client, [{"title": "a", "brief": "a"}], attempts_max=3, max_concurrent=1)
    seen: dict = {}

    async def spent(cid, attempt, text):
        return CALLS, "(stopped: token budget spent)", "budget"

    p = await _run(client, monkeypatch, {"i1": spent}, seen)
    i1 = _by_id(p)["i1"]
    assert len(seen["i1"]) == 1 and i1["status"] == "todo" and i1["attempts"] == 0
    assert i1["continues"] == 0 and i1["history"][-1]["outcome"] == "budget"
    assert p["status"] == "paused"


# --- the retry brief ----------------------------------------------------------

def test_the_added_retry_brief_stays_under_its_cap_and_keeps_the_latest_calls():
    plan = plan_mod.empty_plan(title="t")
    it = plan_mod.new_item(plan, title="big", brief="b")
    it["history"] = [{"attempt": 1, "outcome": "cap", "error": "ran out of rounds (60)",
                      "progress": "", "conversation_id": 1}]
    it["trail"] = {"attempt": 1, "stop": "cap",
                   "calls": [f"run_code {'x' * 70} #{n}" for n in range(20)],
                   "read": [f"src/pkg/module_{n}/{'y' * 30}.py" for n in range(30)],
                   "wrote": [f"src/out/file_{n}.py" for n in range(30)]}
    sec = _section(plan_mod._item_task(plan, it, [], handoff="note " * 600))
    assert len(sec) <= plan_mod.RETRY_BRIEF_CHARS
    assert "(+" in sec                               # the path lists are cut, and say so
    assert "#19" in sec                              # the most recent call survives
    assert "note note" in sec and "## Its handoff note" in sec


def test_a_retry_with_no_trail_still_says_to_continue_and_report():
    plan = plan_mod.empty_plan(title="t")
    it = plan_mod.new_item(plan, title="big", brief="b")
    it["history"] = [{"attempt": 1, "outcome": "stalled", "error": "stalled: quiet",
                      "progress": "", "conversation_id": 1}]
    sec = _section(plan_mod._item_task(plan, it, []))
    assert "Attempt 1: stalled: quiet" in sec and "call plan_report" in sec
    assert "last tool calls" not in sec and "Files it" not in sec
    assert "Continue from where" not in plan_mod._item_task(
        plan, plan_mod.new_item(plan, title="fresh"), [])


# --- blocked on a host capability --------------------------------------------

async def test_a_capability_block_is_stored_and_shown_apart_from_a_failure(
        client, tmp_env, monkeypatch):
    await _put(client, [{"title": "needs a browser", "brief": "b"},
                        {"title": "after it", "brief": "b", "depends_on": ["i1"]},
                        {"title": "needs the operator", "brief": "b"},
                        {"title": "plain failure", "brief": "b"}],
               attempts_max=2, max_concurrent=1)
    seen: dict = {}

    async def cap_block(cid, attempt, text):
        out = await plan_mod.report(SLUG, cid=cid, item_id=None, status="blocked",
                                    summary="no browser on this host", blocked_on="capability")
        assert out.startswith("recorded"), out
        return CALLS, "blocked", None

    async def op_block(cid, attempt, text):
        await plan_mod.report(SLUG, cid=cid, item_id=None, status="blocked",
                              summary="needs the API key")
        return [], "blocked", None

    async def fails(cid, attempt, text):
        await plan_mod.report(SLUG, cid=cid, item_id=None, status="failed", summary="broke")
        return [], "x", None

    p = await _run(client, monkeypatch,
                   {"i1": cap_block, "i3": op_block, "i4": fails}, seen)
    its = _by_id(p)
    i1 = its["i1"]
    assert len(seen["i1"]) == 1, "a capability block is not retried"
    assert i1["status"] == "blocked" and i1["blocked_on"] == "capability"
    assert i1["last_error"] == "blocked on a host capability: no browser on this host"
    assert i1["history"][-1]["blocked_on"] == "capability"
    assert its["i2"]["status"] == "blocked" and its["i2"]["last_error"].startswith("dependency ")
    assert its["i3"]["status"] == "blocked" and its["i3"]["blocked_on"] is None
    assert its["i3"]["last_error"] == "needs the API key"
    assert its["i4"]["status"] == "failed" and its["i4"]["blocked_on"] is None

    text = await plan_mod.status(SLUG)
    assert "i1 [blocked: host capability]" in text
    assert "i3 [blocked]" in text and "i4 [failed]" in text
    assert "(1 of the blocked need a host capability)" in text
    assert "cannot be fixed by a retry" in text
    assert "blocked on a host capability: no browser on this host" in text
    assert "# Blocked on a host capability" in await plan_mod.status(SLUG, item="i1")
    assert "# Blocked on a host capability" not in await plan_mod.status(SLUG, item="i3")
    assert "[blocked: host capability] i1" in plan_mod._listing(p)
    assert "i1 [blocked: host capability]" in plan_mod.render_checklist(p)
    assert plan_mod._listing(p).count("host capability") == 2    # state + its error

    # a retry or a reset starts the item clean
    await plan_mod.fix(SLUG, action="retry", item="i1", guidance="use the HTTP fixtures",
                       run=False)
    again = _by_id(plan_mod.load(SLUG))["i1"]
    assert again["status"] == "todo" and again["blocked_on"] is None
    assert again["continues"] == 0


async def test_blocked_on_is_checked_and_the_fenced_json_fallback_carries_it(
        client, tmp_env, monkeypatch):
    out = await plan_mod.report(SLUG, cid=5, item_id=None, status="done", summary="s",
                                blocked_on="capability")
    assert out == 'error: blocked_on only goes with status "blocked"'
    out = await plan_mod.report(SLUG, cid=5, item_id=None, status="blocked", summary="s",
                                blocked_on="money")
    assert out == "error: blocked_on must be one of operator, capability"

    await _put(client, [{"title": "a", "brief": "b"}], attempts_max=2, max_concurrent=1)
    seen: dict = {}

    async def fenced(cid, attempt, text):
        return [], ('```json\n{"status": "blocked", "blocked_on": "capability", '
                    '"summary": "no desktop variant"}\n```'), None

    p = await _run(client, monkeypatch, {"i1": fenced}, seen)
    i1 = _by_id(p)["i1"]
    assert i1["status"] == "blocked" and i1["blocked_on"] == "capability"
    assert i1["last_error"] == "blocked on a host capability: no desktop variant"


def test_the_retry_trail_stays_out_of_the_published_item_and_survives_the_file():
    plan = plan_mod.empty_plan(title="t")
    it = plan_mod.new_item(plan, title="x")
    it["trail"] = {"attempt": 1, "stop": "cap", "calls": ["a"], "read": [], "wrote": []}
    it["continues"], it["blocked_on"] = 1, "capability"
    plan["items"].append(it)
    assert "trail" not in plan_mod._public_item(it)
    assert all("trail" not in i for i in plan_mod.public(plan, "x")["items"])
    again = plan_mod.normalise(dict(plan))["items"][0]
    assert again["trail"]["calls"] == ["a"] and again["continues"] == 1
    assert again["blocked_on"] == "capability"
