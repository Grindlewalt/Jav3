"""music_control: transport on the operator's TARMAC player."""
from backend import tarmac

SAID = {"pause": "paused", "resume": "resumed",
        "next": "skipped to the next track", "prev": "went back"}


async def run(action: str = "") -> str:
    if action not in ("pause", "resume", "next", "prev"):
        return ("error: action must be pause, resume, next or prev "
                "(TARMAC has no 'stop', and uses 'prev' not 'previous')")
    try:
        r = await tarmac.remote(action)
    except tarmac.TarmacError as e:
        return f"error: {e}"
    n = r.get("players", 0)
    return f"{SAID[action]} on {n} player{'s' if n != 1 else ''}."
