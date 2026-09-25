"""desk_open: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(url: str = "", app: str = "", computer: str = "") -> str:
    return await desk.act("open", {"url": url, "app": app}, computer or None)
