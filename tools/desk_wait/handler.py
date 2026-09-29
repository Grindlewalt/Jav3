"""desk_wait: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(mode: str = "stable", timeout_ms: int = 3000, computer: str = "") -> str:
    return await desk.act("wait", {"mode": mode, "timeout_ms": timeout_ms}, computer or None)
