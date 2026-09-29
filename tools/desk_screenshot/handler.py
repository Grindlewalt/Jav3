"""desk_screenshot: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(monitor: str = "", region: dict | list | str | None = None,
              elements: bool = True, computer: str = "") -> str:
    params: dict = {"monitor": monitor}
    if region not in (None, "", {}, []):
        params["region"] = region
    if elements is False:
        params["elements"] = False
    return await desk.act("screenshot", params, computer or None)
