"""computer_playback: transport control over whatever is playing (MPRIS)."""
from backend import computeruse as cu

_SAID = {"play": "resumed", "pause": "paused", "playpause": "toggled",
         "next": "skipped to the next track", "previous": "went back",
         "stop": "stopped"}


async def run(action: str = "", client: str = "") -> str:
    try:
        r = await cu.dispatch("transport", {"action": action}, client or None)
    except cu.VerbError as e:
        return f"error: {e}"
    if not r.get("ok"):
        return f"error: {r.get('error')}"
    players = r.get("result", {}).get("players") or []
    who = ", ".join(p.rsplit(".", 1)[-1] for p in players)
    return f"{_SAID.get(action, action)}" + (f" ({who})." if who else ".")
