"""desk_screenshot: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(monitor: str = "", computer: str = "") -> str:
    return await desk.act("screenshot", {"monitor": monitor}, computer or None)
