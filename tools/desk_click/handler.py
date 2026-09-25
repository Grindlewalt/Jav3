"""desk_click: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(x: int, y: int, button: str = "left", count: int = 1, computer: str = "") -> str:
    return await desk.act("click", {"x": x, "y": y, "button": button, "count": count}, computer or None)
