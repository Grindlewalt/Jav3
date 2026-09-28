"""desk_click: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(x: int | None = None, y: int | None = None, element: int | None = None,
              target: str = "", button: str = "left", count: int = 1,
              computer: str = "") -> str:
    params = {"button": button, "count": count}
    for k, v in (("x", x), ("y", y), ("element", element), ("target", target)):
        if v not in (None, ""):
            params[k] = v
    return await desk.act("click", params, computer or None)
