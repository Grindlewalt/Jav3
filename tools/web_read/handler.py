from backend import summarize, webtools
from backend.agent.tools.toolctx import active_slug

EXTRACT_SYSTEM = (
    "Extract exactly what the request asks for from this page text. Return only "
    "the extracted content, quotes or facts, no preamble. If the page does not "
    "contain it, say so in one line.")


async def run(url: str, extract: str = "") -> str:
    session = (await active_slug()) or "global"
    text = await webtools.read(url, session)
    if isinstance(extract, str) and extract.strip() and not text.startswith("error:"):
        try:
            answer = await summarize.complete_text(
                EXTRACT_SYSTEM, f"Request: {extract}\n\nURL: {url}\n\n{text}")
            return f"{url}\n\n{answer}"
        except Exception:  # noqa: BLE001 — extraction is best-effort, raw text still answers
            pass
    return text
