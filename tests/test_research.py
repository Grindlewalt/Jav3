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


def test_parse_groups_filters_to_valid_urls():
    from backend.research import _parse_groups
    valid = {"https://a.com", "https://b.com"}
    text = '''here you go:
    [{"theme": "one", "urls": ["https://a.com", "https://evil.com"]},
     {"theme": "two", "urls": ["https://b.com"]}]'''
    groups = _parse_groups(text, valid)
    assert len(groups) == 2
    assert groups[0]["urls"] == ["https://a.com"]   # evil.com (not in results) dropped
    assert groups[1]["urls"] == ["https://b.com"]


def test_parse_groups_bad_json_returns_empty():
    from backend.research import _parse_groups
    assert _parse_groups("no json here", {"https://a.com"}) == []


def test_dom():
    from backend.research import _dom
    assert _dom("https://www.example.com/path") == "example.com"
    assert _dom("https://en.wikipedia.org/wiki/X") == "en.wikipedia.org"
