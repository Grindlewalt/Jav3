"""deploy_agents workers: a real round cap, and a spent one said in the rollup.

The funnel's leaf workers ran under the 12-round subagent fence with no way to
ask for more, and a worker that hit it came back as an ordinary summary
(2026-09-30 benchmark run, same cause as the spawn_temp_agent deaths). Workers
now get the temp-agent default (30, at most 60) and the job's rollup names any
that stopped short.
"""
import contextlib
import importlib.util
from pathlib import Path

import pytest

from backend import orchestrator, turnstats
from backend.config import settings
from backend.db import get_db, init_db

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
async def env(tmp_env, monkeypatch):
    await init_db()

    @contextlib.asynccontextmanager
    async def no_workspace(project, *, top_level):
        yield
    monkeypatch.setattr(orchestrator, "job_workspace", no_workspace)

    async def two_workers(brief, kind):
        return [{"kind": "subagent", "title": "audit the api"},
                {"kind": "subagent", "title": "audit the cli"}]

    async def rollup(brief, output):
        return f"summary: {output[:40]}"

    async def write(project, path, data):
        return None
    monkeypatch.setattr(orchestrator, "_decompose", two_workers)
    monkeypatch.setattr(orchestrator, "_rollup", rollup)
    monkeypatch.setattr(orchestrator, "apply_write", write)
    # workers only: a head with no budget left would run DIRECT, which is not this test
    return monkeypatch


def _workers(monkeypatch, *, spend: dict):
    """Fake worker loops. `spend` maps a worker's brief to the stop its turn
    records; every cap given to the loop is collected."""
    caps = []

    async def turn(cid, sysp, history, *, max_iterations=None, **kw):
        caps.append(max_iterations)
        stop = spend.get(history[0]["content"], "final")
        await turnstats.record(cid, f"guest:{cid}", {
            "type": "turn_stats", "rounds": max_iterations if stop == "cap" else 3, "stop": stop})
        yield {"type": "final", "content": f"did {history[0]['content']}"}
    monkeypatch.setattr(orchestrator, "run_agent_turn", turn)
    return caps


async def _head_rollup(job_id):
    db = await get_db()
    try:
        async with db.execute("SELECT rollup FROM conversations WHERE job_id = ? AND kind = 'head'",
                              (job_id,)) as cur:
            return (await cur.fetchone())["rollup"]
    finally:
        await db.close()


async def test_workers_get_the_temp_default_not_the_12_round_fence(env):
    caps = _workers(env, spend={})
    out = await orchestrator.run_job("job-a", "audit it", "")
    assert caps == [settings.temp_agent_default_rounds] * 2
    assert settings.temp_agent_default_rounds > settings.subagent_max_iterations
    assert "did not finish" not in out["rollup"]            # a clean job says nothing extra


async def test_max_rounds_is_passed_down_and_clamped(env):
    caps = _workers(env, spend={})
    await orchestrator.run_job("job-b", "audit it", "", max_rounds=45)
    assert caps == [45, 45]
    caps.clear()
    await orchestrator.run_job("job-c", "audit it", "", max_rounds=5000)
    assert caps == [settings.plan_item_max_iterations] * 2


async def test_a_worker_that_ran_out_of_rounds_is_named_in_the_rollup(env):
    _workers(env, spend={"audit the cli": "cap"})
    out = await orchestrator.run_job("job-d", "audit it", "", max_rounds=20)
    n = 20
    assert "Note: 1 worker(s) did not finish" in out["rollup"]
    assert f"- audit the cli: ran out of rounds ({n}/{n}) before finishing; partial work" in out["rollup"]
    assert "audit the api" not in out["rollup"].split("Note:")[1]
    assert "max_rounds (up to 60)" in out["rollup"]
    assert await _head_rollup("job-d") == out["rollup"]      # the stored head rollup says it too
    # the worker's own rollup is marked as well, so the tree never shows it as done
    db = await get_db()
    try:
        async with db.execute("SELECT rollup FROM conversations WHERE job_id = 'job-d' "
                              "AND kind != 'head'") as cur:
            rollups = [r["rollup"] for r in await cur.fetchall()]
    finally:
        await db.close()
    assert len(rollups) == 2
    assert [r for r in rollups if r.startswith("(ran out of rounds (20/20)")] and \
        [r for r in rollups if r.startswith("summary:")]


async def test_a_worker_whose_tools_were_withdrawn_is_named_without_a_rounds_hint(env):
    _workers(env, spend={"audit the api": "dead_end"})
    out = await orchestrator.run_job("job-e", "audit it", "")
    assert "- audit the api: stopped early" in out["rollup"]
    assert "max_rounds" not in out["rollup"]


async def test_deploy_agents_takes_max_rounds_and_states_it(env, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "t_deploy", REPO / "tools" / "deploy_agents" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = {}

    async def fake_run_job(job_id, brief, project, **kw):
        seen.update(kw)
        return {"root_id": 1, "rollup": "r"}
    monkeypatch.setattr(mod, "run_job", fake_run_job)

    async def project():
        return "alpha"
    monkeypatch.setattr(mod, "require_project", project)
    await mod.run("do it")
    assert "max_rounds" not in seen                       # the default is the job's own
    await mod.run("do it", max_rounds=40)
    assert seen["max_rounds"] == 40
    import re
    import yaml
    text = (REPO / "tools" / "deploy_agents" / "TOOL.md").read_text()
    front = yaml.safe_load(re.match(r"^---\s*\n(.*?)\n---", text, re.S).group(1))
    prop = front["parameters"]["properties"]["max_rounds"]
    assert prop["type"] == "integer"
    assert str(settings.temp_agent_default_rounds) in prop["description"]
    assert str(settings.plan_item_max_iterations) in prop["description"]
