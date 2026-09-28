"""desk_drag: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(x: int, y: int, to_x: int, to_y: int, button: str = "left",
              computer: str = "") -> str:
    return await desk.act("drag", {"x": x, "y": y, "to_x": to_x, "to_y": to_y,
                                   "button": button}, computer or None)
