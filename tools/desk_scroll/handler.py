"""desk_scroll: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(dy: int = 0, dx: int = 0, x: int | None = None, y: int | None = None,
              element: int | None = None, computer: str = "") -> str:
    params = {k: v for k, v in {"dx": dx, "dy": dy, "x": x, "y": y,
                                "element": element}.items() if v not in (None, "")}
    return await desk.act("scroll", params, computer or None)
