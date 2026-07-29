"""computer_open_link: open a URL in the operator's real browser."""
from backend import computeruse as cu


async def run(url: str = "", screen: int | None = None, client: str = "") -> str:
    params: dict = {"url": url}
    if screen is not None:
        params["screen"] = screen
    try:
        r = await cu.dispatch("open_link", params, client or None)
    except cu.VerbError as e:
        return f"error: {e}"
    if not r.get("ok"):
        return f"error: {r.get('error')}"
    note = (r.get("result") or {}).get("note")
    return f"opened {url} on the operator's computer." + (f" {note}" if note else "")
