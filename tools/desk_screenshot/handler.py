"""desk_screenshot: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(monitor: str = "", region: dict | list | str | None = None,
              elements: bool | str = True, computer: str = "") -> str:
    """elements: true (the front app in full, background windows capped),
    false (no list), "all" (every window in full)."""
    params: dict = {"monitor": monitor}
    if region not in (None, "", {}, []):
        params["region"] = region
    if elements is not True:
        params["elements"] = elements
    return await desk.act("screenshot", params, computer or None)
