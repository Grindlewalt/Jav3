"""browser_navigate: one browser action, gated host-side by backend/browser.py."""
from backend import browser as _browser


async def run(tab: int, url: str, browser: str = "") -> str:
    params = {k: v for k, v in {"tab": tab, "url": url}.items() if v is not None}
    return await _browser.act("navigate", params, browser or None)
