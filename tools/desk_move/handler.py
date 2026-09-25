"""desk_move: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(x: int, y: int, computer: str = "") -> str:
    return await desk.act("move", {"x": x, "y": y}, computer or None)
