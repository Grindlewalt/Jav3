"""desk_type: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(text: str, computer: str = "") -> str:
    return await desk.act("type", {"text": text}, computer or None)
