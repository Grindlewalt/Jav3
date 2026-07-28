"""Disposable self-copies: spawn_temp_agent (2026-07-28). No AGENT.md, no
roster entry — a role prompt on Jarvis's context, run once, report + memory
note survive."""
import importlib.util
from pathlib import Path

from backend import agents_run, runtime
from backend.autonomy import MAX_SPAWN_DEPTH, NON_DELEGABLE, allows

REPO = Path(__file__).resolve().parents[1]


def _names(specs):
    return {s["function"]["name"] for s in specs}


def test_temp_def_lean_vs_duplicate():
    lean = agents_run._temp_agent_def("You are a builder.", False, "builder")
    assert set(lean["context_exclude"]) == set(agents_run.TEMP_LEAN_EXCLUDE)
    assert "soul.md" in lean["context_exclude"]
    assert lean["name"] == "builder"
    dup = agents_run._temp_agent_def("You are a builder.", True)
    assert dup["context_exclude"] == []           # a full copy of Jarvis
    assert dup["name"] == "temp agent"
    # the report-back contract rides on top of every temp role prompt
    for d in (lean, dup):
        assert d["prompt"].startswith("You are a builder.")
        assert "memory_write" in d["prompt"]


def test_temp_toolset_below_and_at_cap():
    agent = agents_run._temp_agent_def("x", False)
    names = _names(agents_run._agent_tools(agent))
    assert "spawn_temp_agent" in names and "spawn_agent" in names   # depth 0
    assert "memory_write" in names                # the report-back channel
    for t in NON_DELEGABLE - {"spawn_agent", "spawn_temp_agent"}:
        assert t not in names                     # infra tools never
    tok = runtime.spawn_depth.set(MAX_SPAWN_DEPTH)
    try:
        capped = _names(agents_run._agent_tools(agent))
        assert "spawn_temp_agent" not in capped and "spawn_agent" not in capped
    finally:
        runtime.spawn_depth.reset(tok)


def test_temp_spawn_is_gated_tier():
    assert allows("gated", "spawn_temp_agent")
    assert not allows("stage", "spawn_temp_agent")


def _load_handler():
    path = REPO / "tools" / "spawn_temp_agent" / "handler.py"
    spec = importlib.util.spec_from_file_location("t_spawn_temp", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_handler_refuses_empty_brief():
    mod = _load_handler()
    assert "error" in await mod.run(task="", prompt="You are x.")
    assert "error" in await mod.run(task="do y", prompt="  ")
