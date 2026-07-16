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


async def test_triage_skips_low_value_pages(tmp_env, monkeypatch):
    await init_db()
    summarized = []

    async def fake_read(url, session):
        return f"PAGE for {url} " + ("word " * 500)

    async def fake_triage(text, url, focus=""):
        return "cookie boilerplate" if "junk" in url else ""

    async def fake_sum(text, url, focus=""):
        summarized.append(url)
        return f"- summary of {url}"

    monkeypatch.setattr(webtools, "read", fake_read)
    monkeypatch.setattr(summarize, "triage_page", fake_triage)
    monkeypatch.setattr(summarize, "summarize_page", fake_sum)

    out = await registry.dispatch("read_and_summarize", {
        "urls": ["http://good.com", "http://junk.com"],
        "focus": "prices", "triage": True})
    assert "summary of http://good.com" in out
    assert "skipped by triage: cookie boilerplate" in out
    assert summarized == ["http://good.com"]      # skipped page never summarized


async def test_triage_off_by_default(tmp_env, monkeypatch):
    await init_db()
    async def fake_read(url, session):
        return "text " * 10

    async def boom(text, url, focus=""):
        raise AssertionError("triage must not run unless requested")

    async def fake_sum(text, url, focus=""):
        return "- ok"

    monkeypatch.setattr(webtools, "read", fake_read)
    monkeypatch.setattr(summarize, "triage_page", boom)
    monkeypatch.setattr(summarize, "summarize_page", fake_sum)
    out = await registry.dispatch("read_and_summarize", {"url": "http://x.com"})
    assert "Source: http://x.com" in out and "- ok" in out


async def test_triage_page_verdict_parsing(tmp_env, monkeypatch):
    """triage_page sends only the head of the page and fails open on noise."""
    calls = []

    async def fake_complete(system, user, temperature=0.3):
        calls.append(user)
        if "skipme" in user:
            return "SKIP: paywall stub"
        if "noise" in user:
            return "well, maybe, hard to say"
        return "KEEP"

    monkeypatch.setattr(summarize, "complete_text", fake_complete)
    summarize._triage_cache.clear()

    long_text = "HEAD MARKER " + ("x" * 5000)
    assert await summarize.triage_page(long_text, "http://keep.com", "f") == ""
    assert len(calls[-1]) < 1200                     # only the head was sent
    assert await summarize.triage_page("skipme", "http://skip.com") == "paywall stub"
    assert await summarize.triage_page("noise", "http://noise.com") == ""  # fail open


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
