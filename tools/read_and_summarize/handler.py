import asyncio

from backend import summarize, webtools
from backend.agent.tools.toolctx import active_slug

MAX_URLS = 8


async def run(urls=None, url=None, focus: str = "") -> str:
    session = (await active_slug()) or "global"
    targets = []
    if isinstance(urls, list):
        targets += [u for u in urls if isinstance(u, str) and u.strip()]
    if isinstance(urls, str) and urls.strip():
        targets.append(urls)
    if isinstance(url, str) and url.strip():
        targets.append(url)
    # de-dupe, preserve order, cap
    seen, ordered = set(), []
    for u in targets:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    ordered = ordered[:MAX_URLS]
    if not ordered:
        return "error: provide a url or a list of urls"

    async def one(u: str) -> str:
        try:
            text = await webtools.read(u, session)
        except Exception as e:  # noqa: BLE001 - surface, don't crash the run
            return f"Source: {u}\n(could not read: {e})"
        if text.startswith("error:") or not text.strip():
            return f"Source: {u}\n({text[:150] or 'empty page'})"
        try:
            s = await summarize.summarize_page(text, u, focus)
        except Exception as e:  # noqa: BLE001
            return f"Source: {u}\n(could not summarize: {e})"
        return f"Source: {u}\n{s}"

    blocks = await asyncio.gather(*(one(u) for u in ordered))
    return "\n\n".join(blocks)
