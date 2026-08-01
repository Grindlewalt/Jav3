"""open_website: open a URL in the browser the operator is actually using.

One tab, not all of them — the same fix as the players. A link opening
simultaneously on the laptop, the desktop and the phone is the browser
equivalent of music starting everywhere.
"""
from backend import gui, runtime


async def run(url: str = "", tab: str = "") -> str:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "error: only http(s) URLs can be opened"
    target, where = gui.resolve_tab(tab or None, runtime.gui_tab.get())
    if target is None:
        return (f"{where}, so nothing opened. Give the operator the URL "
                f"instead: {url}")
    if gui.push({"type": "open_url", "url": url}, tab=target) == 0:
        return f"'{where}' closed before it could open. The URL is {url}"
    return (f"opened on {where}. If the popup blocker intervened the operator "
            "sees a clickable toast instead.")
