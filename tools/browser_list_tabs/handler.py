"""browser_list_tabs: one browser action, gated host-side by backend/browser.py."""
from backend import browser as _browser


async def run(browser: str = "") -> str:
    params: dict = {}
    return await _browser.act("list_tabs", params, browser or None)
