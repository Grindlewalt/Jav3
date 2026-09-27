"""browser_screenshot_tab: one browser action, gated host-side by backend/browser.py."""
from backend import browser as _browser


async def run(tab: int, browser: str = "") -> str:
    params = {k: v for k, v in {"tab": tab}.items() if v is not None}
    return await _browser.act("screenshot_tab", params, browser or None)
