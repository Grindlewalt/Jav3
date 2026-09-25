"""desk_shell: one computer-use action, gated host-side by backend/desk.py."""
from backend import desk


async def run(cmd: str, cwd: str = "", timeout: int = 60, computer: str = "") -> str:
    return await desk.act("shell", {"cmd": cmd, "cwd": cwd or None, "timeout": timeout}, computer or None)
