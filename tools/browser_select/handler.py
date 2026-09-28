"""browser_select: one browser action, gated host-side by backend/browser.py."""
from backend import browser as _browser


async def run(tab: int, element: str, value: str | None = None, label: str | None = None,
              browser: str = "") -> str:
    params = {k: v for k, v in {"tab": tab, "element": element, "value": value,
                                "label": label}.items() if v is not None}
    return await _browser.act("select", params, browser or None)
