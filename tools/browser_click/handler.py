"""browser_click: one browser action, gated host-side by backend/browser.py."""
from backend import browser as _browser


async def run(tab: int, element: int, browser: str = "") -> str:
    params = {k: v for k, v in {"tab": tab, "element": element}.items() if v is not None}
    return await _browser.act("click", params, browser or None)
