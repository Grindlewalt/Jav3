"""browser_open_tab: one browser action, gated host-side by backend/browser.py."""
from backend import browser as _browser


async def run(url: str, browser: str = "") -> str:
    params = {k: v for k, v in {"url": url}.items() if v is not None}
    return await _browser.act("open_tab", params, browser or None)
