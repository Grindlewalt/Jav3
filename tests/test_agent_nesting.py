"""Depth-capped agent nesting + create_agent update path (2026-07-23)."""
import importlib.util
from pathlib import Path

import pytest

from backend import agents_run, runtime
from backend.agents_api import _read
from backend.autonomy import MAX_SPAWN_DEPTH, NON_DELEGABLE

REPO = Path(__file__).resolve().parents[1]


def _names(specs):
    return {s["function"]["name"] for s in specs}


def test_agent_gets_spawn_below_the_cap():
    names = _names(agents_run._agent_tools({}))
    assert "spawn_agent" in names                       # depth 0
    for t in NON_DELEGABLE - {"spawn_agent"}:
        assert t not in names                           # infra tools never


def test_agent_is_leaf_at_the_cap():
    tok = runtime.spawn_depth.set(MAX_SPAWN_DEPTH)
    try:
        assert "spawn_agent" not in _names(agents_run._agent_tools({}))
    finally:
        runtime.spawn_depth.reset(tok)


def test_agent_own_exclusion_wins():
    specs = agents_run._agent_tools({"tools_exclude": ["spawn_agent"]})
    assert "spawn_agent" not in _names(specs)


def _load_create_agent():
    path = REPO / "tools" / "create_agent" / "handler.py"
    spec = importlib.util.spec_from_file_location("t_create_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_create_then_update(tmp_env):
    mod = _load_create_agent()
    out = await mod.run(name="News scout", prompt="# Context\nYou fetch news.")
    assert "created agent 'news-scout'" in out
    # duplicate without update refuses, points at update=true
    out = await mod.run(name="News scout", prompt="different")
    assert "already exists" in out and "update=true" in out
    # give it a model override to prove update preserves operator knobs
    p = _read("news-scout")
    from backend.agents_api import SaveAgent, _write
    _write("news-scout", SaveAgent(**{**p, "model": "llama3"}))
    out = await mod.run(name="News scout", prompt="# Context\nYou fetch WEATHER.",
                        update=True)
    assert "updated agent 'news-scout'" in out
    after = _read("news-scout")
    assert "WEATHER" in after["prompt"] and after["model"] == "llama3"


async def test_update_missing_refuses(tmp_env):
    mod = _load_create_agent()
    out = await mod.run(name="ghost", prompt="x", update=True)
    assert "no agent 'ghost' to update" in out
