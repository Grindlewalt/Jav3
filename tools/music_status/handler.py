"""music_status: what the music server and both its players are doing.

Two destinations report separately and neither can see the other: TARMAC knows
about its own PWA players, and the in-page Jarvis player reports to the host
directly. Merging them into one "now playing" would be a guess, so both are
stated.
"""
from backend import gui, tarmac


def _at(pos, dur) -> str:
    if not (isinstance(pos, (int, float)) and isinstance(dur, (int, float)) and dur):
        return ""
    return (f" at {int(pos // 60)}:{int(pos % 60):02d} "
            f"of {int(dur // 60)}:{int(dur % 60):02d}")


def _in_page_lines() -> list[str]:
    s = gui.player_status()
    tabs = s.get("tabs", 0)
    track = s.get("track") or {}
    if not tabs:
        return ["Jarvis player: no tab open"]
    if not track or s.get("stale"):
        return [f"Jarvis player: idle ({tabs} tab(s) open, ready to play)"]
    who = " — ".join(x for x in (track.get("title"), track.get("artist")) if x)
    state = "paused" if s.get("paused") else "playing"
    if s.get("error"):
        return [f"Jarvis player: loaded {who} but the browser refused to start "
                f"it — {s['error']}"]
    if not s.get("started"):
        return [f"Jarvis player: {who} loaded, no sound confirmed yet"]
    left = s.get("queue") or 0
    tail = f", {left} more queued" if left else ""
    return [f"Jarvis player: {state} {who}"
            f"{_at(s.get('position'), s.get('duration'))}"
            f" at {s.get('volume', 100)}% volume{tail}"]


async def run() -> str:
    lines = _in_page_lines()
    try:
        s = await tarmac.status()
    except tarmac.TarmacError as e:
        # with a tab open the in-page player still works, so an unreachable
        # server is a partial answer rather than a failure. With no tab there is
        # genuinely nothing to report, and the model should read it as an error.
        if gui.player_status().get("tabs"):
            return "\n".join(lines + [f"music server: {e}"])
        return f"error: {e}"

    n = s.get("players_connected", 0)
    lines.insert(0, f"library: {s.get('tracks', '?')} tracks")
    lines.append(
        f"music app players open: {n}"
        + ("" if n else "  (nothing to play on there — but the Jarvis player "
                        "works without it)"))
    np = s.get("now_playing")
    if np and not n:
        # the server remembers its last track after every player closed; read
        # verbatim this sent a voice agent chasing a phantom "already playing"
        who = " — ".join(x for x in (np.get("title"), np.get("artist")) if x)
        lines.append(f"music app: nothing actually playing (no player open — "
                     f"the server just remembers its last track, {who})")
    elif np:
        who = " — ".join(x for x in (np.get("title"), np.get("artist")) if x)
        state = "paused" if np.get("paused") else "playing"
        lines.append(f"music app: {state} {who}"
                     f"{_at(np.get('position'), np.get('duration'))}")
    else:
        lines.append("music app: nothing playing")
    return "\n".join(lines)
