"""Host-side web access core: SearXNG search + inert page fetch + a shared
fetch ledger so multiple agents don't scrape the same page (wasting tokens
and narrowing the diversity of what's gathered).

Everything here runs on the trusted host. When Jarvis moves into the VM, these
become the host proxy the VM calls — the VM never opens a raw socket to the
internet; it only ever receives the sanitised text these functions return.
"""
import datetime
import time
from urllib.parse import urlparse

import httpx

from .config import settings
from .db import get_db
from .websec import UnsafeURL, html_to_text, is_safe_url

# Browser-like headers: many sites 403 a bare/unknown agent or a request with
# no Accept headers. This fetcher only reads public pages and returns inert text.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/125.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# --- short-TTL content cache ---------------------------------------------
# key -> (expires_at_monotonic, text). Shared helpers: summarize.py keeps its
# own dict of summaries keyed (url, focus) with the same TTL/size settings.
_page_cache: dict[str, tuple[float, str]] = {}


def _cache_get(cache: dict, key) -> str | None:
    now = time.monotonic()
    for k in [k for k, (exp, _) in cache.items() if exp <= now]:
        del cache[k]
    hit = cache.get(key)
    return hit[1] if hit else None


def _cache_put(cache: dict, key, text: str) -> None:
    cache[key] = (time.monotonic() + settings.web_cache_ttl_seconds, text)
    while len(cache) > settings.web_cache_max_entries:
        del cache[min(cache, key=lambda k: cache[k][0])]  # drop closest to expiry


def _same_host(a: str, b: str) -> bool:
    """Hostnames equal (case-insensitive) or differing only by a leading www."""
    ha = (urlparse(a).hostname or "").lower()
    hb = (urlparse(b).hostname or "").lower()
    return ha.removeprefix("www.") == hb.removeprefix("www.")


async def search_results(query: str, limit: int = 6) -> list[dict]:
    """Structured SearXNG results [{url, title, snippet}] — for the research
    pipeline's batch-search phase (as data, not a text blob)."""
    try:
        async with httpx.AsyncClient(timeout=settings.web_fetch_timeout,
                                     http2=True) as c:
            r = await c.get(f"{settings.searxng_url}/search",
                            params={"q": query, "format": "json"}, headers=HEADERS)
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    for res in (data.get("results") or [])[:limit]:
        url = res.get("url")
        if url:
            out.append({"url": url, "title": res.get("title", ""),
                        "snippet": (res.get("content") or "").strip()[:300]})
    return out


async def search(query: str, session: str) -> str:
    """Query SearXNG, return a compact text list of results. Results already
    pulled in this session are flagged so agents pick fresh sources."""
    try:
        async with httpx.AsyncClient(timeout=settings.web_fetch_timeout,
                                     http2=True) as c:
            r = await c.get(f"{settings.searxng_url}/search",
                            params={"q": query, "format": "json"},
                            headers=HEADERS)
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return f"error: search backend unreachable: {e}"

    seen = await fetched_set(session)
    lines = [f"search: {query}",
             f"(today is {datetime.date.today():%Y-%m-%d} — include the current "
             "year in queries about recent events)"]
    for ans in (data.get("answers") or [])[:3]:
        text = ans if isinstance(ans, str) else ans.get("answer", "")
        if text:
            lines.append(f"[answer] {text}")
    for box in (data.get("infoboxes") or [])[:1]:
        if box.get("content"):
            lines.append(f"[infobox] {box['content'][:400]}")
    results = data.get("results") or []
    if not results:
        lines.append("(no results)")
    for i, res in enumerate(results[:settings.web_search_results], 1):
        url = res.get("url", "")
        flag = "  [already fetched — pick a different source]" if url in seen else ""
        lines.append(f"\n{i}. {res.get('title', '(no title)')}\n   {url}{flag}\n"
                     f"   {(res.get('content') or '').strip()[:280]}")
    lines.append("\nCite sources in your answer as markdown links using the URLs above.")
    return "\n".join(lines)


async def read(url: str, session: str) -> str:
    """Fetch a page and return inert plain text. SSRF-guarded. Claims the URL
    in the shared ledger BEFORE fetching, so parallel bots never pull the same
    page; a failed fetch releases the claim so it can be retried.

    {{secret:NAME}} placeholders are substituted for the fetch ONLY — and only
    onto the secret's bound hosts (see secrets.substitute_url). The ledger,
    cache, and everything returned to the model keep the placeholder form;
    fetched bodies and error strings are scrubbed because APIs love echoing
    the key back in error payloads."""
    from . import secrets as secrets_mod
    fetch_url = url
    if "{{secret:" in url:
        try:
            fetch_url = secrets_mod.substitute_url(url)
        except (KeyError, ValueError) as e:
            return f"error: {str(e).strip(chr(39))}"
    substituted = fetch_url != url

    def _out(text: str) -> str:
        return secrets_mod.scrub(text) if substituted else text

    try:
        is_safe_url(fetch_url)
    except UnsafeURL as e:
        return _out(f"error: refused to fetch — {e}")

    # still record the claim on a cache hit: the ledger keeps flagging the URL
    # as fetched in search results (source diversity), but the re-read is free
    claimed = await claim(session, url)
    cached = _cache_get(_page_cache, url)
    if cached is not None:
        return cached + "\n\n(served from cache)"
    if not claimed:
        return (f"note: {url} was already claimed this session (by you or "
                "another bot). Pick a different source to diversify — or say "
                "why you need it again.")

    ok = False
    try:
        async with httpx.AsyncClient(timeout=settings.web_fetch_timeout,
                                     http2=True) as c:
            # manual redirect loop: same-host 3xx are followed (SSRF-rechecked
            # each hop); a cross-host redirect is handed back to the model so
            # an open redirect can't be laundered into a fetch we never vetted
            current, raw, ctype = fetch_url, b"", ""
            for _ in range(10):
                async with c.stream("GET", current, headers=HEADERS) as r:
                    if 300 <= r.status_code < 400:
                        loc = r.headers.get("location")
                        if not loc:
                            return _out(f"error: {current} answered with redirect "
                                        f"status {r.status_code} but no Location header")
                        target = str(httpx.URL(current).join(loc))
                        if not _same_host(current, target):
                            return _out(f"error: not followed — {url} redirects to a "
                                        f"different host: {target}. If that destination "
                                        "is what you want, call the tool again with that URL.")
                        try:
                            is_safe_url(target)
                        except UnsafeURL as e:
                            return _out(f"error: refused after redirect — {e}")
                        current = target
                        continue
                    r.raise_for_status()
                    ctype = r.headers.get("content-type", "")
                    chunks, total = [], 0
                    async for chunk in r.aiter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > settings.web_max_bytes:
                            break
                    raw = b"".join(chunks)
                    break
            else:
                return _out(f"error: too many redirects (>10) fetching {url}")
        body = raw.decode("utf-8", errors="replace")
        if "html" in ctype or "<html" in body[:2000].lower():
            title, text = html_to_text(body)
        else:
            title, text = "", body  # plain text / markdown / json served as-is
        text = text[:settings.web_max_chars]
        ok = True
        head = f"# {title}\n{url}\n\n" if title else f"{url}\n\n"
        out = _out(head + (text or "(no readable text extracted)"))
        _cache_put(_page_cache, url, out)  # keyed by the URL as requested
        return out
    except httpx.HTTPError as e:
        return _out(f"error: fetch failed: {e}")
    finally:
        if not ok:
            await release(session, url)  # let a failed/refused fetch be retried


# --- fetch ledger ------------------------------------------------------------

async def fetched_set(session: str) -> set[str]:
    db = await get_db()
    try:
        async with db.execute(
            "SELECT url FROM fetched_urls WHERE session = ?", (session,)) as cur:
            return {r["url"] for r in await cur.fetchall()}
    finally:
        await db.close()


async def claim(session: str, url: str) -> bool:
    """Reserve a URL before fetching. Returns True if this caller got the claim,
    False if another bot already claimed it (INSERT OR IGNORE + rowcount is the
    atomic test-and-set; WAL lets parallel bots race it safely)."""
    from . import runtime
    if runtime.ephemeral.get():
        return True  # incognito: no ledger, no dedup
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT OR IGNORE INTO fetched_urls (session, url) VALUES (?, ?)",
            (session, url))
        await db.commit()
        return cur.rowcount == 1
    finally:
        await db.close()


async def release(session: str, url: str) -> None:
    """Drop a claim so a failed/refused fetch can be retried by anyone."""
    from . import runtime
    if runtime.ephemeral.get():
        return
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM fetched_urls WHERE session = ? AND url = ?", (session, url))
        await db.commit()
    finally:
        await db.close()


async def record(session: str, url: str, title: str) -> None:
    """Legacy record (kept for tests / direct use). claim() is the fetch path."""
    from . import runtime
    if runtime.ephemeral.get():
        return
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO fetched_urls (session, url, title) VALUES (?, ?, ?)",
            (session, url, title))
        await db.commit()
    finally:
        await db.close()
