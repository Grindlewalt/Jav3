"""browser_key: one browser action, gated host-side by backend/browser.py."""
from backend import browser as _browser


async def run(tab: int, combo: str, browser: str = "") -> str:
    params = {k: v for k, v in {"tab": tab, "combo": combo}.items() if v is not None}
    return await _browser.act("key", params, browser or None)
