"""Soft dependencies and the fan-in lint (benchmark-game run, conv 500: one verify
item with 18 hard dependencies blocked everything behind it, 8 of 22 items never
ran, and no integrated build came out).

A soft dependency is satisfied once its item SETTLED, however it ended; the item
runs with whatever finished and its brief lists what did not. Hard dependencies
behave as before. Same offline harness as test_plan.py."""
import json

from backend import agents_run, capabilities
from backend import plan as plan_mod
from tests.test_plan import (SLUG, _by_id, _put, _wait_run, client)  # noqa: F401


def _plan(*specs):
    """specs: (title, depends_on, soft_deps) -> a plan whose items are i1..iN."""
    plan = plan_mod.empty_plan()
    plan["items"] = [plan_mod.new_item(plan, title=t, depends_on=[*d, *s], soft_deps=s)
                     for t, d, s in specs]
    return plan_mod.normalise(plan)


def _st(plan, **status):
    for k, v in status.items():
        _by_id(plan)[k]["status"] = v


# --- the pure resolution ---------------------------------------------------------

def test_a_soft_dependency_is_met_by_any_way_of_ending_a_hard_one_is_not():
    plan = _plan(("a", [], []), ("b", [], []), ("c", [], []),
                 ("join", ["i1"], ["i2", "i3"]))
    assert [i["id"] for i in plan_mod.ready(plan)] == ["i1", "i2", "i3"]
    _st(plan, i1="done", i2="todo", i3="running")
    assert plan_mod.ready(plan) == [_by_id(plan)["i2"]], "soft waits for a dep still todo"
    _st(plan, i2="failed", i3="blocked")
    assert [i["id"] for i in plan_mod.ready(plan)] == ["i4"], "failed and blocked have settled"
    _st(plan, i1="failed")                         # the HARD one failing still blocks it
    assert plan_mod.ready(plan) == []
    assert [i["id"] for i in plan_mod.propagate_blocked(plan)] == ["i4"]
    assert "i1 failed" in _by_id(plan)["i4"]["last_error"]


def test_a_failed_or_blocked_soft_dependency_never_blocks_the_item():
    plan = _plan(("a", [], []), ("b", [], []), ("join", [], ["i1", "i2"]))
    _st(plan, i1="failed", i2="blocked")
    assert plan_mod.propagate_blocked(plan) == []
    assert _by_id(plan)["i3"]["status"] == "todo"
    assert [i["id"] for i in plan_mod.ready(plan)] == ["i3"]


def test_the_hub_case_a_blocked_chain_behind_a_soft_join_still_runs_it():
    # i1 failed -> i2 blocked behind it -> i3 (soft after both) runs; i4 hard after i3
    plan = _plan(("part", [], []), ("part 2", ["i1"], []), ("verify", [], ["i1", "i2"]),
                 ("ship", ["i3"], []))
    _st(plan, i1="failed")
    plan_mod.propagate_blocked(plan)
    assert _by_id(plan)["i2"]["status"] == "blocked"
    assert [i["id"] for i in plan_mod.ready(plan)] == ["i3"]


def test_making_a_dependency_soft_releases_an_item_blocked_by_it():
    plan = _plan(("part", [], []), ("verify", ["i1"], []))
    _st(plan, i1="failed")
    plan_mod.propagate_blocked(plan)
    it = _by_id(plan)["i2"]
    assert it["status"] == "blocked"
    plan_mod.apply_item_edit(plan, it, {"soft_deps": ["i1"]})
    assert [i["id"] for i in plan_mod.release_blocked(plan)] == ["i2"]
    assert it["status"] == "todo" and plan_mod.ready(plan) == [it]


def test_soft_deps_survive_normalise_and_only_mean_something_on_a_live_edge():
    plan = _plan(("a", [], []), ("b", [], []), ("join", ["i1"], ["i2"]))
    again = plan_mod.normalise(json.loads(json.dumps(plan)))
    assert _by_id(again)["i3"]["soft_deps"] == ["i2"]
    assert plan_mod.hard_deps(_by_id(again)["i3"]) == ["i1"]
    # a soft mark on an id that is not a dependency (or a deleted one) is dropped
    raw = {"items": [{"id": "i1", "title": "a"},
                     {"id": "i2", "title": "j", "depends_on": ["i1"], "soft_deps": ["i1", "i9"]},
                     {"id": "i3", "title": "k", "soft_deps": ["i1"]}]}
    norm = _by_id(plan_mod.normalise(raw))
    assert norm["i2"]["soft_deps"] == ["i1"] and norm["i3"]["soft_deps"] == []
    # a file from before soft dependencies loads with every dependency hard
    old = {"items": [{"id": "i1", "title": "a"}, {"id": "i2", "title": "b", "depends_on": ["i1"]}]}
    assert _by_id(plan_mod.normalise(old))["i2"]["soft_deps"] == []


def test_soft_deps_edit_replaces_the_soft_set_and_adds_unlisted_dependencies():
    plan = _plan(("a", [], []), ("b", [], []), ("join", ["i1"], []))
    it = _by_id(plan)["i3"]
    plan_mod.apply_item_edit(plan, it, {"soft_deps": ["i2"]})
    assert it["depends_on"] == ["i1", "i2"] and it["soft_deps"] == ["i2"]
    plan_mod.apply_item_edit(plan, it, {"soft_deps": []})
    assert plan_mod.hard_deps(it) == ["i1", "i2"], "an empty list makes every dependency hard"
    plan_mod.apply_item_edit(plan, it, {"soft_deps": ["i1", "i2"], "depends_on": ["i1"]})
    assert it["soft_deps"] == ["i1", "i2"] and it["depends_on"] == ["i1", "i2"]


def test_the_checklist_shows_which_dependencies_are_soft():
    plan = _plan(("a", [], []), ("b", [], []), ("join", ["i1"], ["i2"]), ("end", [], ["i3"]))
    text = plan_mod.render_checklist(plan)
    assert "join (after i1; soft: i2)" in text and "end (soft: i3)" in text
    assert "soft (runs after they settle" in plan_mod._item_text(_by_id(plan)["i3"])


# --- the brief -----------------------------------------------------------------------

def _gap_brief(plan, item_id):
    idx = _by_id(plan)
    it = idx[item_id]
    return plan_mod._item_task(plan, it, [idx[d] for d in it["depends_on"]])


def test_the_brief_lists_the_soft_dependencies_that_did_not_finish():
    plan = _plan(("walls", [], []), ("sound", [], []), ("menus", ["i1"], []),
                 ("pickups", [], []), ("verify build", [], ["i1", "i2", "i3", "i4"]))
    _st(plan, i1="done", i2="failed", i3="blocked", i4="blocked")
    i1, i2, i3, i4 = (_by_id(plan)[k] for k in ("i1", "i2", "i3", "i4"))
    i1["result_summary"] = "walls.js ok"
    i2.update(attempts=2, last_error="ran out of rounds (40) before calling plan_report",
              result_summary="half the sound table")
    i3.update(attempts=0, last_error="dependency i1 failed")
    i4.update(attempts=1, blocked_on="capability", last_error="blocked on a host capability: no browser")
    text = _gap_brief(plan, "i5")
    assert "## i1 — walls (done)\nwalls.js ok" in text
    gaps = text[text.index("# Dependencies that did not finish"):]
    assert "i3 menus: never ran (dependency i1 failed). Its part is missing." in gaps
    assert "i2 sound: failed after 2 attempt(s) (ran out of rounds" in gaps
    assert "What it reported: half the sound table" in gaps
    assert "i4 pickups: blocked on a host capability after 1 attempt(s)" in gaps
    assert "list the gaps in your plan_report summary" in gaps
    # the unfinished ones are not also shown as results
    assert "## i2" not in text and "## i3" not in text


def test_a_brief_with_everything_done_has_no_gap_section_and_hard_deps_never_get_one():
    plan = _plan(("a", [], []), ("b", [], []), ("join", ["i1"], ["i2"]))
    _st(plan, i1="done", i2="done")
    assert "did not finish" not in _gap_brief(plan, "i3")
    _st(plan, i2="skipped")
    assert "did not finish" not in _gap_brief(plan, "i3"), "skipped satisfies even a hard one"


# --- the planner and the runner ------------------------------------------------------

async def test_the_planner_writes_soft_dependencies_with_after_soft(client, monkeypatch):
    seen = {}

    async def fake_complete(system, user, temperature=0.3):
        seen["system"] = system
        return json.dumps([
            {"title": "walls", "brief": "w"}, {"title": "sound", "brief": "s"},
            {"title": "menus", "brief": "m", "depends_on": [0]},
            {"title": "build it all", "brief": "b", "depends_on": [0, "walls"],
             "after_soft": [1, "i3", "menus", 0, 7]}])
    monkeypatch.setattr(plan_mod, "complete_text", fake_complete)
    plan = await plan_mod.plan_from_dump(SLUG, "make the game")
    last = _by_id(plan)["i4"]
    assert plan_mod.hard_deps(last) == ["i1"]
    assert last["soft_deps"] == ["i2", "i3"], "soft: indices, ids and titles; a hard id stays hard"
    assert "after_soft" in seen["system"] and "never in depends_on" in seen["system"]
    assert _by_id(plan_mod.load(SLUG))["i4"]["soft_deps"] == ["i2", "i3"], "persisted"


def _script(done: set, seen: dict):
    async def turn(cid, system_prompt, history, **kw):
        info = plan_mod.live_item(cid)
        seen.setdefault(info["item_id"], []).append(history[0]["content"])
        if info["item_id"] in done:
            await plan_mod.report(SLUG, cid=cid, item_id=None, status="done",
                                  summary=f"{info['item_id']} built")
            yield {"type": "final", "content": "done"}
        else:
            yield {"type": "final", "content": "I gave up."}
    return turn


async def test_a_join_item_runs_with_what_finished_and_hard_dependants_stay_blocked(
        client, monkeypatch):
    # i2 fails for good, i3 sits behind it (hard) and never runs; the soft join i4
    # runs anyway with a brief naming both; i5 hangs on i2 hard and stays blocked
    await _put(client, [
        {"title": "walls", "brief": "w"},
        {"title": "sound", "brief": "s"},
        {"title": "menus", "brief": "m", "depends_on": ["i2"]},
        {"title": "verify build", "brief": "v", "depends_on": ["i1", "i2", "i3"],
         "soft_deps": ["i1", "i2", "i3"]},
        {"title": "polish", "brief": "p", "depends_on": ["i2"]},
    ], attempts_max=1, max_concurrent=2)
    seen: dict = {}
    monkeypatch.setattr(agents_run, "run_agent_turn", _script({"i1", "i4"}, seen))

    async def synth(system, user, temperature=0.3):
        return "ROLLUP"
    monkeypatch.setattr(plan_mod, "complete_text", synth)
    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    plan = plan_mod.load(SLUG)
    items = _by_id(plan)
    assert items["i1"]["status"] == "done"
    assert items["i2"]["status"] == "failed" and items["i3"]["status"] == "blocked"
    assert items["i3"]["attempts"] == 0
    assert items["i4"]["status"] == "done", "the join ran although two of its three parts did not"
    assert items["i5"]["status"] == "blocked" and "i2 failed" in items["i5"]["last_error"]
    brief = seen["i4"][0]
    assert "i1 built" in brief
    assert "i2 sound: failed after 1 attempt(s)" in brief
    assert "i3 menus: never ran (dependency i2 failed)" in brief
    assert plan["status"] == "failed", "the run still reports the parts that failed"


# --- the lint ----------------------------------------------------------------------------

def test_lint_flags_fan_in_past_eight_hard_dependencies():
    plan = _plan(*[(f"part {n}", [], []) for n in range(10)],
                 ("assemble", [f"i{n}" for n in range(1, 10)], []),
                 ("eight", [f"i{n}" for n in range(1, 9)], []))
    [w] = plan_mod.lint(plan)
    assert w.startswith('i11 "assemble" 9 hard dependencies (i1, i2, i3, i4, i5, i6, ...)')
    assert "soft" in w and "reports the gaps" in w, "the way out is named"
    assert plan_mod.lint(plan, only={"i12"}) == [], "exactly eight is allowed"


def test_lint_flags_verify_ship_and_integrate_items_with_hard_dependencies():
    plan = _plan(("a", [], []), ("b", [], []), ("c", [], []),
                 ("Verify the build end to end", ["i1", "i2"], []),
                 ("Ship it", ["i1"], []),                 # one hard dep: just a successor
                 ("Integrate the parts", ["i1", "i2", "i3"], []),
                 ("Final verification", [], ["i1", "i2", "i3"]),   # soft: fine
                 ("Write the docs", ["i1", "i2", "i3"], []))        # not a join by its title
    got = plan_mod.lint(plan)
    assert [w.split()[0] for w in got] == ["i4", "i6"]
    assert "verifies/integrates/ships other items' work and has 2 hard dependencies" in got[0]


def test_lint_open_only_skips_items_that_have_run_and_keeps_dependency_blocked_ones():
    plan = _plan(*[(f"part {n}", [], []) for n in range(3)],
                 ("Verify all", ["i1", "i2", "i3"], []),
                 ("Verify again", ["i1", "i2", "i3"], []),
                 ("Verify once more", ["i1", "i2", "i3"], []))
    _st(plan, i1="failed", i2="done", i3="done", i4="done")
    plan_mod.propagate_blocked(plan)
    assert _by_id(plan)["i5"]["status"] == "blocked"
    _by_id(plan)["i6"].update(status="blocked", last_error="needs the operator's key")
    assert [w.split()[0] for w in plan_mod.lint(plan)] == ["i4", "i5", "i6"]
    assert [w.split()[0] for w in plan_mod.lint(plan, open_only=True)] == ["i5"]
    assert plan_mod.public(plan, "x")["warnings"] == plan_mod.lint(plan, open_only=True)


# --- plan_fix, plan_status ------------------------------------------------------------------

async def test_plan_fix_add_and_edit_take_soft_deps_and_warn_about_a_fan_in(client):
    await _put(client, [{"title": f"part {n}", "brief": "p"} for n in range(1, 11)])
    ids = [f"i{n}" for n in range(1, 11)]
    out = await plan_mod.fix(SLUG, action="add", title="assemble everything", brief="join",
                             depends_on=ids, run=False)
    assert out.startswith("added i11") and "Plan warnings" in out
    assert 'i11 "assemble everything" 10 hard dependencies' in out
    out = await plan_mod.fix(SLUG, action="edit", item="i11", soft_deps=ids, run=False)
    assert out.startswith("i11 edited") and "Plan warnings" not in out
    assert _by_id(plan_mod.load(SLUG))["i11"]["soft_deps"] == ids
    out = await plan_mod.fix(SLUG, action="add", title="verify soft", brief="v",
                             soft_deps=["i1", "i2"], run=False)
    assert out.startswith("added i12") and "Plan warnings" not in out
    it = _by_id(plan_mod.load(SLUG))["i12"]
    assert it["depends_on"] == ["i1", "i2"] and it["soft_deps"] == ["i1", "i2"]
    assert "no item i99" in await plan_mod.fix(SLUG, action="edit", item="i11",
                                               soft_deps=["i99"], run=False)
    assert "no item i99" in await plan_mod.fix(SLUG, action="add", title="x", brief="y",
                                               soft_deps=["i99"], run=False)


async def test_plan_fix_soft_deps_frees_an_item_the_runner_blocked(client):
    await _put(client, [{"title": "part", "brief": "p"},
                        {"title": "verify", "brief": "v", "depends_on": ["i1"]}])
    async with plan_mod.edit(SLUG) as p:
        _by_id(p)["i1"]["status"] = "failed"
        plan_mod.propagate_blocked(p)
    assert plan_mod.load(SLUG)["items"][1]["status"] == "blocked"
    out = await plan_mod.fix(SLUG, action="edit", item="i2", soft_deps=["i1"], run=False)
    assert "Released: i2" in out
    assert plan_mod.load(SLUG)["items"][1]["status"] == "todo"


async def test_plan_status_warns_about_unrun_items_and_names_the_way_out(client, monkeypatch):
    monkeypatch.setattr(capabilities, "for_project", lambda slug: _caps({}))
    await _put(client, [{"title": f"part {n}", "brief": "p"} for n in range(1, 4)]
               + [{"title": "Verify everything", "brief": "v", "depends_on": ["i1", "i2", "i3"]}])
    text = await plan_mod.status(SLUG)
    assert "Plan warnings" in text and 'i4 "Verify everything"' in text
    async with plan_mod.edit(SLUG) as p:
        _by_id(p)["i1"]["status"] = "failed"
        for i in ("i2", "i3"):
            _by_id(p)[i]["status"] = "done"
        plan_mod.propagate_blocked(p)
    text = await plan_mod.status(SLUG)
    assert "i4 blocked only because a hard dependency failed or never ran" in text
    assert "soft_deps" in text and "Plan warnings" in text
    async with plan_mod.edit(SLUG) as p:
        _by_id(p)["i4"]["soft_deps"] = ["i1", "i2", "i3"]
        plan_mod.release_blocked(p)
    assert "Plan warnings" not in await plan_mod.status(SLUG)


async def _caps(caps):
    return caps
