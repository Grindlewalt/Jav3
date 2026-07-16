"""Summarize-on-read: the compaction primitive, shared by the research pipeline
and the read_and_summarize tool.

The point is token economy in a ReAct loop. A full web page (~6k chars) that
lands in the message array gets re-sent on every subsequent iteration — the
quadratic blow-up. Summarizing the page *inside* the tool means the full text
is spent exactly once (this model call) and only a few bullets ride along in
the loop. The internal call shares the operation's Budget contextvar, so it's
metered like everything else.
"""
# re-exported so `summarize.complete_text` stays the shared entry point (and
# stays monkeypatchable in tests); the implementation lives at the model choke point
from .agent.model import complete_text
from .config import settings
from .webtools import _cache_get, _cache_put

# (url, focus) -> summary, same TTL/size settings as the page cache: a cached
# page re-summarized with the same focus skips the model call too
_summary_cache: dict[tuple[str, str], tuple[float, str]] = {}

# (url, focus) -> skip-reason or "" (keep); triage verdicts are cheap but so is
# caching them, and a re-read within the TTL shouldn't flip-flop
_triage_cache: dict[tuple[str, str], tuple[float, str]] = {}

# how much of the page the triage skim sees — roughly the first paragraph or
# two of stripped text; the whole point is spending ~1k chars to decide whether
# to spend web_max_chars on the full summary
TRIAGE_HEAD_CHARS = 1_000


async def triage_page(text: str, url: str, focus: str = "") -> str:
    """Skim the head of a page and decide whether it's worth summarizing.

    Returns "" to keep the page, or a short reason string to skip it. Fails
    open: anything unparseable keeps the page — wrongly skipping loses
    information, wrongly keeping only costs one summary call.
    """
    key = (url, focus)
    cached = _cache_get(_triage_cache, key)
    if cached is not None:
        return cached
    lens = (f"information relevant to: {focus}" if focus.strip()
            else "substantive informational content")
    out = await complete_text(
        "You skim the opening of a web page and judge whether the full page is "
        f"worth reading for {lens}. Error pages, paywalls, login walls, cookie "
        "boilerplate, link farms and pages plainly about something else are not "
        "worth reading. Reply with exactly one line: KEEP, or "
        "SKIP: <short reason>.",
        f"URL: {url}\n\nOpening of the page:\n{text[:TRIAGE_HEAD_CHARS]}",
        temperature=0.0)
    verdict = out.strip().splitlines()[0] if out.strip() else "KEEP"
    if verdict.upper().startswith("SKIP"):
        reason = verdict.split(":", 1)[1].strip() if ":" in verdict else "low value"
        result = reason or "low value"
    else:
        result = ""          # KEEP (or anything unrecognized — fail open)
    _cache_put(_triage_cache, key, result)
    return result


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
