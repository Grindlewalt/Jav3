"""browser_scroll: one browser action, gated host-side by backend/browser.py."""
from backend import browser as _browser


async def run(tab: int, pages: int | None = None, browser: str = "") -> str:
    params = {k: v for k, v in {"tab": tab, "pages": pages}.items() if v is not None}
    return await _browser.act("scroll", params, browser or None)
