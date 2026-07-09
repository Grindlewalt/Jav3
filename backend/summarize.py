"""Summarize-on-read: the compaction primitive, shared by the research pipeline
and the read_and_summarize tool.

The point is token economy in a ReAct loop. A full web page (~6k chars) that
lands in the message array gets re-sent on every subsequent iteration — the
quadratic blow-up. Summarizing the page *inside* the tool means the full text
is spent exactly once (this model call) and only a few bullets ride along in
the loop. The internal call shares the operation's Budget contextvar, so it's
metered like everything else.
"""
from .agent.model import model
from .config import settings
from .webtools import _cache_get, _cache_put

# (url, focus) -> summary, same TTL/size settings as the page cache: a cached
# page re-summarized with the same focus skips the model call too
_summary_cache: dict[tuple[str, str], tuple[float, str]] = {}


async def complete_text(system: str, user: str, temperature: float = 0.3) -> str:
    parts = []
    async for ev in model.complete(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}], temperature=temperature):
        if ev["type"] == "message":
            parts.append(ev["content"])
    return "".join(parts).strip()


async def summarize_page(text: str, url: str, focus: str = "") -> str:
    key = (url, focus)
    cached = _cache_get(_summary_cache, key)
    if cached is not None:
        return cached
    lens = f"what is relevant to: {focus}" if focus.strip() else "the key facts"
    out = await complete_text(
        f"Summarize {lens} from this page in 3-6 tight bullet points. Only facts "
        "stated on the page. No preamble, no fluff.",
        f"URL: {url}\n\n{text[:settings.web_max_chars]}")
    _cache_put(_summary_cache, key, out)
    return out
