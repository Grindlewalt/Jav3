"""browser_read_page: one browser action, gated host-side by backend/browser.py."""
from backend import browser as _browser


async def run(tab: int, max_chars: int | None = None, wait_ms: int | None = None,
              min_elements: int | None = None, selector: str | None = None,
              browser: str = "") -> str:
    # every parameter TOOL.md offers is accepted here: wait_ms/min_elements/
    # selector were advertised but refused ("unexpected keyword"), 2026-09-27
    params = {k: v for k, v in {"tab": tab, "max_chars": max_chars, "wait_ms": wait_ms,
                                "min_elements": min_elements,
                                "selector": selector}.items() if v is not None}
    return await _browser.act("read_page", params, browser or None)
