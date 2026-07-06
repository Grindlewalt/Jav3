"""Research orchestration: registration + helpers (full flow needs the live API)."""
from backend.agent.tools import registry


def test_research_tool_registered(tmp_env):
    names = {e["name"] for e in registry.compile_registry()}
    assert "research" in names
    granted = {s["function"]["name"] for s in registry.openai_tool_specs()}
    assert "research" in granted


def test_slugify():
    from backend.research import _slugify
    assert _slugify("Raspberry Pi 5 vs 4!") == "raspberry-pi-5-vs-4"
    assert _slugify("") == "topic"
    assert len(_slugify("x" * 100)) <= 50
