"""browser_type: one browser action, gated host-side by backend/browser.py."""
from backend import browser as _browser


async def run(tab: int, element: int, text: str, submit: bool | None = None, browser: str = "") -> str:
    params = {k: v for k, v in {"tab": tab, "element": element, "text": text, "submit": submit}.items() if v is not None}
    return await _browser.act("type", params, browser or None)
