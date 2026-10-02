"""Round caps on spawned agents (2026-09-30 benchmark run, convs 571-573).

spawn_temp_agent's child ran under a silent 12-round fence: all three big
delegations died at 12-13 calls with "Tool budget ran out before the deliverables
were written", and the parent had no way to ask for more nor to tell a spent cap
from a failure. These pin: the cap is set (default, caller's max_rounds, clamped
to the plan-item maximum), the TOOL.md states it, and a spent cap is said plainly.
"""
import importlib.util
import re
from pathlib import Path

import yaml

from backend import agents_run, turnstats
from backend.config import settings
from backend.db import init_db
from backend.memory import ensure_memory_seeds

REPO = Path(__file__).resolve().parents[1]


def _handler(tool: str):
    spec = importlib.util.spec_from_file_location(
        f"t_rounds_{tool}", REPO / "tools" / tool / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tool_md(tool: str):
    text = (REPO / "tools" / tool / "TOOL.md").read_text()
    front = yaml.safe_load(re.match(r"^---\s*\n(.*?)\n---", text, re.S).group(1))
    return front, text.split("\n---\n", 1)[1]


async def _ready():
    await init_db()
    ensure_memory_seeds()


def _capture_turn(monkeypatch) -> dict:
    seen = {}

    async def fake_turn(cid, sysp, hist, **kw):
        seen.update(kw)
        yield {"type": "final", "content": "ok"}
    monkeypatch.setattr(agents_run, "run_agent_turn", fake_turn)
    return seen


def _ends_on(monkeypatch, stop: str, content: str, rounds=None):
    """A turn the loop ends for `stop`: it records its turn_stats row (what the
    guest path does) and answers."""
    async def fake_turn(cid, sysp, hist, *, max_iterations=None, **kw):
        await turnstats.record(cid, f"guest:{cid}", {
            "type": "turn_stats", "rounds": rounds or max_iterations, "stop": stop})
        yield {"type": "final", "content": content}
    monkeypatch.setattr(agents_run, "run_agent_turn", fake_turn)


def test_temp_def_always_carries_a_round_cap():
    # 0 fell back to the 12-round subagent fence
    default = agents_run._temp_agent_def("x", False)["max_iterations"]
    assert default == settings.temp_agent_default_rounds > settings.subagent_max_iterations
    top = settings.plan_item_max_iterations
    for given, want in [(45, 45), (top, top), (top + 500, top), (1, 1),
                        (0, default), (-3, default), (None, default), ("junk", default)]:
        assert agents_run._temp_agent_def("x", False, "", given)["max_iterations"] == want


async def test_headless_temp_run_gets_the_default_then_the_asked_cap(tmp_env, monkeypatch):
    await _ready()
    seen = _capture_turn(monkeypatch)
    await agents_run.run_temp_agent_headless("You are a worker.", "do it", active=None)
    assert seen["max_iterations"] == settings.temp_agent_default_rounds
    await agents_run.run_temp_agent_headless("You are a worker.", "do it", active=None,
                                             max_rounds=45)
    assert seen["max_iterations"] == 45
    await agents_run.run_temp_agent_headless("You are a worker.", "do it", active=None,
                                             max_rounds=10_000)
    assert seen["max_iterations"] == settings.plan_item_max_iterations


async def test_a_spent_round_cap_is_said_plainly_to_the_parent(tmp_env, monkeypatch):
    await _ready()
    _ends_on(monkeypatch, "cap", "drafted 1 of 2 files")
    spawn = _handler("spawn_temp_agent")
    out = await spawn.run(task="build it", prompt="You build.")
    n = settings.temp_agent_default_rounds
    assert f"ran out of rounds ({n}/{n}) before finishing; partial work:" in out
    assert "drafted 1 of 2 files" in out and "max_rounds" in out
    assert "reports]" not in out                  # not dressed as a finished report
    out = await spawn.run(task="build it", prompt="You build.", max_rounds=50)
    assert "ran out of rounds (50/50)" in out


async def test_a_finished_run_reads_as_a_report(tmp_env, monkeypatch):
    await _ready()
    _capture_turn(monkeypatch)
    out = await _handler("spawn_temp_agent").run(task="build it", prompt="You build.")
    assert out.startswith("[temp agent reports]\nok")


async def test_other_early_stops_are_named_too(tmp_env, monkeypatch):
    await _ready()
    spawn = _handler("spawn_temp_agent")
    _ends_on(monkeypatch, "dead_end", "could not log in", rounds=5)
    out = await spawn.run(task="t", prompt="p")
    assert "stopped early" in out and "could not log in" in out and "ran out" not in out

    # an incognito turn leaves no turn_stats row: the loop's own cut-off text says it
    async def cut_off(cid, sysp, hist, **kw):
        yield {"type": "final", "content": agents_run.CAP_FINAL + " without finishing)"}
    monkeypatch.setattr(agents_run, "run_agent_turn", cut_off)
    assert "ran out of rounds" in await spawn.run(task="t", prompt="p")


async def test_spawn_agent_takes_max_rounds_and_says_a_spent_cap(tmp_env, monkeypatch):
    await _ready()
    d = settings.agents_dir / "scout"
    d.mkdir(parents=True)
    (d / "AGENT.md").write_text("---\nname: scout\ndescription: d\n---\nYou are scout.")
    seen = _capture_turn(monkeypatch)
    spawn = _handler("spawn_agent")
    await spawn.run(agent="scout", task="look")
    assert seen["max_iterations"] == settings.subagent_max_iterations   # the stated default
    await spawn.run(agent="scout", task="look", max_rounds=40)
    assert seen["max_iterations"] == 40
    await spawn.run(agent="scout", task="look", max_rounds=999)
    assert seen["max_iterations"] == settings.plan_item_max_iterations
    _ends_on(monkeypatch, "cap", "half")
    out = await spawn.run(agent="scout", task="look", max_rounds=20)
    assert "ran out of rounds (20/20) before finishing; partial work:" in out


def test_tool_md_states_the_default_and_the_maximum():
    front, body = _tool_md("spawn_temp_agent")
    prop = front["parameters"]["properties"]["max_rounds"]
    assert prop["type"] == "integer"
    assert str(settings.temp_agent_default_rounds) in prop["description"]
    assert str(settings.plan_item_max_iterations) in prop["description"]
    assert str(settings.temp_agent_default_rounds) in body and "max_rounds" in body
    front, body = _tool_md("spawn_agent")
    assert front["parameters"]["properties"]["max_rounds"]["type"] == "integer"
    assert str(settings.subagent_max_iterations) in body
