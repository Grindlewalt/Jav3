"""Web tools: SSRF guard, HTML sanitisation, the fetch ledger, registration."""
import pytest

from backend.agent.tools import registry
from backend.db import get_db, init_db
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
    await webtools.record("proj", "https://a.com", "A")
    await webtools.record("proj", "https://a.com", "A")   # idempotent
    await webtools.record("other", "https://b.com", "B")
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
