"""music_status: what the music server and its players are doing."""
from backend import tarmac


async def run() -> str:
    try:
        s = await tarmac.status()
    except tarmac.TarmacError as e:
        return f"error: {e}"
    n = s.get("players_connected", 0)
    lines = [f"library: {s.get('tracks', '?')} tracks",
             f"players open: {n}" + ("" if n else "  (nothing to play on — the "
                                     "operator needs to open the music app)")]
    np = s.get("now_playing")
    if np:
        who = " — ".join(x for x in (np.get("title"), np.get("artist")) if x)
        state = "paused" if np.get("paused") else "playing"
        pos = np.get("position")
        dur = np.get("duration")
        at = ""
        if isinstance(pos, (int, float)) and isinstance(dur, (int, float)) and dur:
            at = f" at {int(pos // 60)}:{int(pos % 60):02d} of {int(dur // 60)}:{int(dur % 60):02d}"
        lines.append(f"{state}: {who}{at}")
    else:
        lines.append("nothing playing")
    return "\n".join(lines)
