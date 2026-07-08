"""read_and_summarize tool: reads many pages in one call, returns only the
summaries (full pages never enter context). Model + fetch are monkeypatched so
this runs offline."""
from backend import summarize, webtools
from backend.agent.tools import registry
from backend.db import init_db


async def test_batches_dedupes_and_returns_only_summaries(tmp_env, monkeypatch):
    await init_db()
    fetched = []

    async def fake_read(url, session):
        fetched.append(url)
        return f"FULL PAGE TEXT for {url} " + ("lorem " * 2000)

    async def fake_sum(text, url, focus=""):
        assert "FULL PAGE TEXT" in text          # got the full page to summarize
        return f"- summary of {url} (focus={focus})"

    monkeypatch.setattr(webtools, "read", fake_read)
    monkeypatch.setattr(summarize, "summarize_page", fake_sum)

    out = await registry.dispatch("read_and_summarize", {
        "urls": ["http://a.com", "http://b.com", "http://a.com"],  # dup
        "focus": "prices"})
    assert "summary of http://a.com" in out and "summary of http://b.com" in out
    assert "focus=prices" in out
    assert "FULL PAGE TEXT" not in out           # full text never returned
    assert fetched.count("http://a.com") == 1     # de-duped
    assert out.count("Source:") == 2


async def test_caps_at_eight(tmp_env, monkeypatch):
    await init_db()
    async def fake_read(url, session):
        return "text " * 10

    async def fake_sum(text, url, focus=""):
        return "- ok"

    monkeypatch.setattr(webtools, "read", fake_read)
    monkeypatch.setattr(summarize, "summarize_page", fake_sum)
    out = await registry.dispatch("read_and_summarize",
                                  {"urls": [f"http://s{i}.com" for i in range(20)]})
    assert out.count("Source:") == 8


async def test_no_urls_errors(tmp_env):
    await init_db()
    out = await registry.dispatch("read_and_summarize", {})
    assert out.startswith("error:")


async def test_read_failure_is_surfaced_not_fatal(tmp_env, monkeypatch):
    await init_db()
    async def boom(url, session):
        raise RuntimeError("ssrf blocked")

    async def fake_sum(text, url, focus=""):
        return "- ok"

    monkeypatch.setattr(webtools, "read", boom)
    monkeypatch.setattr(summarize, "summarize_page", fake_sum)
    out = await registry.dispatch("read_and_summarize", {"url": "http://x.com"})
    assert "could not read" in out and "ssrf blocked" in out
