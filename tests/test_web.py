"""Web tools: SSRF guard, HTML sanitisation, the fetch ledger, registration."""
import pytest

from backend.agent.tools import registry
from backend.db import init_db
from backend.websec import UnsafeURL, html_to_text, is_safe_url
from backend import webtools


def test_ssrf_guard_refuses_internal():
    for bad in ("http://127.0.0.1/", "http://localhost/", "http://10.0.0.58:8080/",
                "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/",
                "file:///etc/passwd", "ftp://example.com/"):
        with pytest.raises(UnsafeURL):
            is_safe_url(bad)


def test_ssrf_guard_allows_public_ip():
    # 1.1.1.1 is a global address and needs no DNS
    assert is_safe_url("https://1.1.1.1/") == "https://1.1.1.1/"


def test_html_to_text_strips_active_content():
    html = """<html><head><title>Hi There</title>
    <style>.x{color:red}</style></head><body>
    <script>alert('evil'); fetch('http://10.0.0.58')</script>
    <h1>Heading</h1><p>First para.</p><p>Second &amp; last.</p>
    <nav>menu junk</nav></body></html>"""
    title, text = html_to_text(html)
    assert title == "Hi There"
    assert "Heading" in text and "First para." in text and "Second & last." in text
    assert "alert" not in text and "evil" not in text and "color:red" not in text
    assert "menu junk" not in text  # nav stripped


async def test_fetch_ledger_roundtrip(tmp_env):
    await init_db()
    assert await webtools.fetched_set("proj") == set()
    assert await webtools.claim("proj", "https://a.com") is True
    assert await webtools.claim("proj", "https://a.com") is False   # idempotent
    assert await webtools.claim("other", "https://b.com") is True
    assert await webtools.fetched_set("proj") == {"https://a.com"}
    assert await webtools.fetched_set("other") == {"https://b.com"}


async def test_read_refuses_unsafe_before_any_fetch(tmp_env):
    await init_db()
    out = await webtools.read("http://169.254.169.254/latest/meta-data/", "proj")
    assert out.startswith("error: refused")
    # nothing recorded for a refused fetch
    assert await webtools.fetched_set("proj") == set()


def test_web_tools_registered(tmp_env):
    names = {e["name"] for e in registry.compile_registry()}
    assert "web_search" in names and "web_read" in names
    granted = {s["function"]["name"] for s in registry.openai_tool_specs()}
    assert "web_search" in granted and "web_read" in granted


# --- the ledger scopes to the OPERATION, not the project (06:45 post-mortem) ---
# Keying claims by project slug made them permanent: a scheduled morning run
# found every feed URL claimed by yesterday's chat turn and starved. The web
# tools now key by runtime.web_session (set per chat turn / agent run / job),
# with the project slug only as an out-of-operation fallback.

async def test_claims_are_per_operation(tmp_env):
    await init_db()
    url = "https://feeds.example.com/rss.xml"
    assert await webtools.claim("turn:1:aaa", url) is True
    assert await webtools.claim("turn:1:aaa", url) is False   # dedup within op
    assert await webtools.claim("run:99", url) is True        # next run is fresh


async def test_web_session_prefers_operation_scope(tmp_env):
    from backend import runtime
    from backend.agent.tools.toolctx import web_session

    await init_db()
    assert await web_session() == "global"          # no op, no project
    tok = runtime.web_session.set("run:42")
    try:
        assert await web_session() == "run:42"
    finally:
        runtime.web_session.reset(tok)


async def test_headless_agent_run_gets_own_session(tmp_env, monkeypatch):
    """Two runs of the same agent must not share a ledger scope."""
    from backend import agents_run
    from backend import runtime as runtime_mod
    from backend.config import settings

    await init_db()
    (settings.agents_dir / "probe").mkdir(parents=True)
    (settings.agents_dir / "probe" / "AGENT.md").write_text(
        "---\nname: probe\ndescription: t\n---\n\nYou are probe.\n")

    seen = []

    async def stub_turn(cid, system_prompt, history, tools=None, **kw):
        seen.append(runtime_mod.web_session.get())
        yield {"type": "final", "content": "ok"}
    monkeypatch.setattr(agents_run, "run_agent_turn", stub_turn)

    await agents_run.run_agent_headless("probe", "go", active=None)
    await agents_run.run_agent_headless("probe", "go", active=None)
    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(s and s.startswith("run:") for s in seen)


def test_search_params_pins_working_engines(monkeypatch):
    """web_search must pin the engines that actually return results — the Pi's
    default SearXNG mix is mostly blocked (0 results). See the 07-21 fix."""
    from backend.config import settings
    monkeypatch.setattr(settings, "searxng_engines", "bing,mojeek,wikipedia")
    p = webtools._search_params("kevin durant")
    assert p["q"] == "kevin durant" and p["format"] == "json"
    assert p["engines"] == "bing,mojeek,wikipedia"
    # empty setting -> let SearXNG choose (no engines param)
    monkeypatch.setattr(settings, "searxng_engines", "")
    assert "engines" not in webtools._search_params("x")
