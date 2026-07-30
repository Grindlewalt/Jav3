"""computer_library: browse the granted folders, one level at a time."""
from backend import computeruse as cu


async def run(folder: str = "", kind: str = "both", limit: int = 60) -> str:
    if kind not in ("both", "audio", "video"):
        return "error: kind must be 'both', 'audio' or 'video'"
    limit = max(1, min(int(limit or 60), 300))
    try:
        return await cu.tree_local(folder or None, kind, limit)
    except cu.VerbError as e:
        return f"error: {e}"
