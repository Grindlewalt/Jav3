"""desk_move: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(x: int | None = None, y: int | None = None, element: int | None = None,
              frame: int | None = None, computer: str = "") -> str:
    params = {k: v for k, v in {"x": x, "y": y, "element": element, "frame": frame}.items()
              if v not in (None, "")}
    return await desk.act("move", params, computer or None)
