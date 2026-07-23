"""open_website: push an open-a-tab action to every connected GUI tab."""
from backend import gui


async def run(url: str = "") -> str:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "error: only http(s) URLs can be opened"
    n = gui.push({"type": "open_url", "url": url})
    if n == 0:
        return ("no GUI tab is connected right now — nothing opened. "
                f"Give the operator the URL instead: {url}")
    return (f"sent to {n} open tab(s). If the popup blocker intervened the "
            "operator sees a clickable toast instead.")
