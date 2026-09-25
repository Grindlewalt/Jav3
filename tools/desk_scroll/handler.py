"""desk_scroll: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(dy: int = 0, dx: int = 0, x: int | None = None, y: int | None = None, computer: str = "") -> str:
    return await desk.act("scroll", {k: v for k, v in {"dx": dx, "dy": dy, "x": x, "y": y}.items() if v is not None}, computer or None)
