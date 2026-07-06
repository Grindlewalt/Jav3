"""Host-side web access core: SearXNG search + inert page fetch + a shared
fetch ledger so multiple agents don't scrape the same page (wasting tokens
and narrowing the diversity of what's gathered).

Everything here runs on the trusted host. When Jarvis moves into the VM, these
become the host proxy the VM calls — the VM never opens a raw socket to the
internet; it only ever receives the sanitised text these functions return.
"""
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
    lines = [f"search: {query}"]
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
    return "\n".join(lines)


async def read(url: str, session: str) -> str:
    """Fetch a page and return inert plain text. SSRF-guarded. Claims the URL
    in the shared ledger BEFORE fetching, so parallel bots never pull the same
    page; a failed fetch releases the claim so it can be retried."""
    try:
        is_safe_url(url)
    except UnsafeURL as e:
        return f"error: refused to fetch — {e}"

    if not await claim(session, url):
        return (f"note: {url} was already claimed this session (by you or "
                "another bot). Pick a different source to diversify — or say "
                "why you need it again.")

    ok = False
    try:
        async with httpx.AsyncClient(timeout=settings.web_fetch_timeout,
                                     follow_redirects=True, http2=True) as c:
            async with c.stream("GET", url, headers=HEADERS) as r:
                # a public URL can 3xx to an internal one — re-check where we
                # actually landed before reading a byte of the body
                try:
                    is_safe_url(str(r.url))
                except UnsafeURL as e:
                    return f"error: refused after redirect — {e}"
                r.raise_for_status()
                ctype = r.headers.get("content-type", "")
                chunks, total = [], 0
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > settings.web_max_bytes:
                        break
                raw = b"".join(chunks)
        body = raw.decode("utf-8", errors="replace")
        if "html" in ctype or "<html" in body[:2000].lower():
            title, text = html_to_text(body)
        else:
            title, text = "", body  # plain text / markdown / json served as-is
        text = text[:settings.web_max_chars]
        ok = True
        head = f"# {title}\n{url}\n\n" if title else f"{url}\n\n"
        return head + (text or "(no readable text extracted)")
    except httpx.HTTPError as e:
        return f"error: fetch failed: {e}"
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
