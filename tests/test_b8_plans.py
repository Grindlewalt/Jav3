"""Backlog B8 (plans): RUNS-11 (a budget stop is not a failed attempt), RUNS-07
(plan_fix accept), RUNS-14 (wait with plan_status). Same offline harness as
test_plan.py: every item's turn is a scripted stand-in for run_agent_turn."""
import pytest

from backend import agents_run
from backend import plan as plan_mod
from backend.config import settings
from tests.test_plan import (SLUG, _by_id, _put, _report, _scripted,  # noqa: F401
                             _wait_run, client)


async def _no_synth(system, user, temperature=0.3):
    return "ROLLUP"


# --- RUNS-11 ---------------------------------------------------------------

BUDGET_FINAL = ("(stopped: token budget spent (49,288,375 in / 1,014,883 out "
                "(cache hit 90%, charged 5,000,000)))")


async def test_budget_stop_pauses_the_run_instead_of_burning_attempts(
        client, tmp_env, monkeypatch):
    """i15-i20 burned 9 attempts in 8 seconds on 2026-09-27: a turn that ended
    '(stopped: token budget spent ...)' looked like an item that forgot to
    plan_report, so it failed and was re-spawned at once."""
    await _put(client, [{"title": "a", "brief": "a"}, {"title": "b", "brief": "b"}],
               attempts_max=3, max_concurrent=1)
    seen: dict = {}

    async def spent(cid, attempt, text):
        return BUDGET_FINAL

    base = _scripted({"i1": spent}, seen)

    async def turn(cid, system_prompt, history, **kw):
        async for ev in base(cid, system_prompt, history, **kw):
            # the loop marks a budget stop on its final event
            yield {**ev, "stop": "budget"} if ev["content"] == BUDGET_FINAL else ev

    monkeypatch.setattr(agents_run, "run_agent_turn", turn)
    monkeypatch.setattr(plan_mod, "complete_text", _no_synth)

    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    p = plan_mod.load(SLUG)
    i1 = _by_id(p)["i1"]
    assert len(seen["i1"]) == 1, "a budget stop must not re-spawn the item"
    assert i1["status"] == "todo" and i1["attempts"] == 0     # not a spent attempt
    assert "i2" not in seen                                   # nothing else started
    assert p["status"] == "paused"
    assert "budget" in p["paused_reason"]
    assert i1["history"][-1]["outcome"] == "budget"


# --- RUNS-07 ---------------------------------------------------------------

async def test_accept_closes_a_verified_item_without_a_dispatch(client, tmp_env, monkeypatch):
    """plan_fix 9164/9246: 'Your work is DONE and verified' re-dispatched whole
    agents (10-15k of brief, 130-240k input tokens) only to file a report."""
    await _put(client, [{"title": "root", "brief": "r"},
                        {"title": "leaf", "brief": "l", "depends_on": ["i1"]}],
               attempts_max=1, max_concurrent=2)
    seen: dict = {}

    async def i1(cid, attempt, text):
        return "built it all, forgot plan_report"        # no report: the item fails

    async def i2(cid, attempt, text):
        await _report(cid, "done", "leaf built")
        return "ok"

    monkeypatch.setattr(agents_run, "run_agent_turn", _scripted({"i1": i1, "i2": i2}, seen))
    monkeypatch.setattr(plan_mod, "complete_text", _no_synth)
    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    items = _by_id(plan_mod.load(SLUG))
    assert items["i1"]["status"] == "failed" and items["i2"]["status"] == "blocked"

    # needs the verification it rests on; a bare accept is refused
    assert (await plan_mod.fix(SLUG, action="accept", item="i1")).startswith("error:")
    out = await plan_mod.fix(SLUG, action="accept", item="i1",
                             summary="verified: npm test passes, 14 files in src/")
    assert "accepted" in out and "Relaunched" in out and "i2" in out
    await _wait_run()
    p = plan_mod.load(SLUG)
    items = _by_id(p)
    assert items["i1"]["status"] == "done" and items["i2"]["status"] == "done"
    assert items["i1"]["result_summary"] == "verified: npm test passes, 14 files in src/"
    assert items["i1"]["report"]["by"] == "orchestrator"
    assert items["i1"]["history"][-1]["outcome"] == "accepted"
    assert items["i1"].get("fixes", 0) == 0          # not a re-dispatch: no cap used
    assert len(seen["i1"]) == 1                       # its agent was never run again
    assert p["status"] == "done"


async def test_accept_refuses_running_and_finished_items(client, tmp_env):
    await _put(client, [{"title": "a", "brief": "a"}, {"title": "b", "brief": "b"}])
    async with plan_mod.edit(SLUG) as plan:
        plan["items"][0]["status"] = "running"
        plan["items"][1]["status"] = "done"
    running = await plan_mod.fix(SLUG, action="accept", item="i1", summary="ok", run=False)
    assert running.startswith("error:") and "running" in running
    done = await plan_mod.fix(SLUG, action="accept", item="i2", summary="ok", run=False)
    assert done.startswith("error:") and "already done" in done
    # the summary may ride in guidance: models put it there
    async with plan_mod.edit(SLUG) as plan:
        plan["items"][0]["status"] = "failed"
    out = await plan_mod.fix(SLUG, action="accept", item="i1", guidance="checked by hand",
                             run=False)
    assert "accepted" in out
    assert _by_id(plan_mod.load(SLUG))["i1"]["result_summary"] == "checked by hand"
