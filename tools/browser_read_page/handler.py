"""browser_read_page: one browser action, gated host-side by backend/browser.py."""
from backend import browser as _browser


async def run(tab: int, max_chars: int | None = None, browser: str = "") -> str:
    params = {k: v for k, v in {"tab": tab, "max_chars": max_chars}.items() if v is not None}
    return await _browser.act("read_page", params, browser or None)
