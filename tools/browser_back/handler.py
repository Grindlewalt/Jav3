"""browser_back: history back (or forward), gated host-side by backend/browser.py."""
from backend import browser as _browser


async def run(tab: int, forward: bool | None = None, browser: str = "") -> str:
    return await _browser.act("forward" if forward is True else "back", {"tab": tab},
                              browser or None)
