"""Web + spawn_agent lessons (punch-list 12/14/15/16/17): short-TTL content
cache, cross-host redirect posture, web_read extract, search prompt fixes,
spawn_agent usage trailer. Network and model are mocked — runs offline."""
import datetime

import httpx
import pytest

from backend import agents_run, summarize, webtools
from backend.agent.budget import Budget, active_budget
from backend.agent.tools import registry
from backend.config import settings
from backend.db import get_db, init_db


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    monkeypatch.setattr(webtools, "_page_cache", {})
    monkeypatch.setattr(summarize, "_summary_cache", {})


def mock_client(monkeypatch, handler):
    """Route webtools' own AsyncClient constructions through a MockTransport."""
    real = httpx.AsyncClient

    def factory(**kw):
        kw.pop("http2", None)
        return real(transport=httpx.MockTransport(handler),
                    timeout=kw.get("timeout"))

    monkeypatch.setattr(webtools.httpx, "AsyncClient", factory)


def page_handler(calls):
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "text/plain"},
                              text="page body")
    return handler


# --- content cache (item 14) --------------------------------------------

async def test_cache_hit_skips_refetch_but_records_claim(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(webtools, "is_safe_url", lambda u: u)
    calls = []
    mock_client(monkeypatch, page_handler(calls))

    first = await webtools.read("https://ex.com/a", "s1")
    assert "page body" in first and "(served from cache)" not in first
    # a different session wins its own claim, but the cache still short-circuits
    second = await webtools.read("https://ex.com/a", "s2")
    assert "page body" in second and second.endswith("(served from cache)")
    assert len(calls) == 1
    assert "https://ex.com/a" in await webtools.fetched_set("s2")


async def test_expired_cache_entry_refetches(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(webtools, "is_safe_url", lambda u: u)
    monkeypatch.setattr(settings, "web_cache_ttl_seconds", 0)
    calls = []
    mock_client(monkeypatch, page_handler(calls))

    await webtools.read("https://ex.com/a", "s1")
    out = await webtools.read("https://ex.com/a", "s2")
    assert len(calls) == 2
    assert "(served from cache)" not in out


async def test_claimed_url_with_warm_cache_serves_content(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(webtools, "is_safe_url", lambda u: u)
    calls = []
    mock_client(monkeypatch, page_handler(calls))

    await webtools.read("https://ex.com/a", "s1")
    again = await webtools.read("https://ex.com/a", "s1")  # claim lost, cache warm
    assert "page body" in again and again.endswith("(served from cache)")
    assert not again.startswith("note:")
    assert len(calls) == 1
    # cold cache + lost claim keeps the refusal note
    webtools._page_cache.clear()
    refused = await webtools.read("https://ex.com/a", "s1")
    assert refused.startswith("note:") and len(calls) == 1


# --- redirect posture (item 17) -------------------------------------------

async def test_cross_host_redirect_errors_and_releases_claim(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(webtools, "is_safe_url", lambda u: u)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/land"})
    mock_client(monkeypatch, handler)

    out = await webtools.read("https://good.com/start", "s")
    assert out.startswith("error: not followed")
    assert "https://evil.example/land" in out
    assert "call the tool again" in out
    # claim released — a retry is not blocked by the ledger
    out2 = await webtools.read("https://good.com/start", "s")
    assert out2.startswith("error: not followed") and len(calls) == 2
    assert await webtools.fetched_set("s") == set()


async def test_same_host_www_redirect_is_followed(tmp_env, monkeypatch):
    await init_db()
    checks = []
    monkeypatch.setattr(webtools, "is_safe_url",
                        lambda u: (checks.append(u), u)[1])
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "www.ex.com":
            return httpx.Response(301, headers={"location": "https://ex.com/real"})
        return httpx.Response(200, headers={"content-type": "text/plain"},
                              text="landed")
    mock_client(monkeypatch, handler)

    out = await webtools.read("https://www.ex.com/", "s")
    assert "landed" in out and not out.startswith("error:")
    assert calls == ["https://www.ex.com/", "https://ex.com/real"]
    assert "https://ex.com/real" in checks  # SSRF re-checked on the hop


# --- web_read extract (item 15) --------------------------------------------

async def test_web_read_extract_uses_small_model(tmp_env, monkeypatch):
    await init_db()

    async def fake_read(url, session):
        return f"{url}\n\nThe price is $5."

    async def fake_complete(system, user, temperature=0.3):
        assert "Extract exactly what the request asks for" in system
        assert "Request: the price" in user and "The price is $5." in user
        return "$5"

    monkeypatch.setattr(webtools, "read", fake_read)
    monkeypatch.setattr(summarize, "complete_text", fake_complete)
    out = await registry.dispatch(
        "web_read", {"url": "https://x.com/p", "extract": "the price"})
    assert out == "https://x.com/p\n\n$5"

    # extraction failure falls back to the raw text
    async def boom(system, user, temperature=0.3):
        raise RuntimeError("no key")
    monkeypatch.setattr(summarize, "complete_text", boom)
    out = await registry.dispatch(
        "web_read", {"url": "https://x.com/p", "extract": "the price"})
    assert "The price is $5." in out

    # no extract -> raw text, no model call
    out = await registry.dispatch("web_read", {"url": "https://x.com/p"})
    assert "The price is $5." in out


# --- search prompt fixes (item 16) ------------------------------------------

async def test_search_output_has_date_line_and_citation_reminder(tmp_env, monkeypatch):
    await init_db()
    payload = {"results": [{"url": "https://a.com/x", "title": "A",
                            "content": "snip"}]}
    mock_client(monkeypatch, lambda request: httpx.Response(200, json=payload))

    out = await webtools.search("test topic", "s")
    today = datetime.date.today().strftime("%Y-%m-%d")
    lines = out.splitlines()
    assert lines[0] == "search: test topic"
    assert lines[1] == (f"(today is {today} — include the current year in "
                        "queries about recent events)")
    assert lines[-1] == ("Cite sources in your answer as markdown links using "
                         "the URLs above.")
    assert "https://a.com/x" in out


# --- spawn_agent trailer (item 12) -------------------------------------------

async def test_spawn_agent_usage_trailer(tmp_env, monkeypatch):
    await init_db()
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO conversations (summary, kind) VALUES ('t', 'agent')")
        cid = cur.lastrowid
        for _ in range(3):
            await db.execute(
                "INSERT INTO tool_calls (conversation_id, tool, args) "
                "VALUES (?, 'x', '{}')", (cid,))
        await db.commit()
    finally:
        await db.close()

    async def fake_run(agent, task):
        active_budget.get().add({"prompt_tokens": 1000, "completion_tokens": 300})
        return {"conversation_id": cid, "agent": "Recon", "final": "found the thing"}
    monkeypatch.setattr(agents_run, "run_agent_headless", fake_run)

    token = active_budget.set(Budget(max_input=10**6, max_output=10**6))
    try:
        out = await registry.dispatch("spawn_agent",
                                      {"agent": "recon", "task": "scan"})
    finally:
        active_budget.reset(token)
    assert out.startswith("[Recon reports]")
    assert "found the thing" in out
    assert out.endswith("(usage: ~1,300 tokens, 3 tool calls)")


async def test_spawn_agent_empty_output_marker(tmp_env, monkeypatch):
    await init_db()

    async def fake_run(agent, task):
        return {"conversation_id": 999, "agent": "Recon", "final": "   "}
    monkeypatch.setattr(agents_run, "run_agent_headless", fake_run)

    out = await registry.dispatch("spawn_agent",
                                  {"agent": "recon", "task": "scan"})
    assert "(agent completed but returned no output.)" in out
    # no budget on the contextvar -> tool-call-only trailer
    assert out.endswith("(usage: 0 tool calls)")
