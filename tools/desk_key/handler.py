"""desk_key: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(combo: str, computer: str = "") -> str:
    return await desk.act("key", {"combo": combo}, computer or None)
