"""music_control: transport on whichever music player is actually playing.

Two players, one vocabulary. The Jarvis in-page player accepts everything here
including volume and stop; TARMAC's own players have neither — its remote API is
pause/resume/next/prev and nothing else. Rather than pretend, `auto` sends the
action to the player that currently holds a track, and volume/stop say plainly
that the music app cannot do them.
"""
from backend import gui, runtime, tarmac

ACTIONS = ("pause", "resume", "next", "prev", "volume", "stop")
IN_PAGE_ONLY = ("volume", "stop")

SAID = {"pause": "paused", "resume": "resumed",
        "next": "skipped to the next track", "prev": "went back",
        "stop": "stopped and closed the player"}


def _resolve_where(where: str) -> str:
    """`auto` follows the sound: if the in-page player is holding a track and
    its tab is still reporting, that is what the operator is listening to."""
    w = (where or "auto").strip().lower()
    if w in ("jarvis", "page", "here", "browser"):
        return "jarvis"
    if w in ("app", "tarmac", "pwa", "phone"):
        return "app"
    s = gui.player_status()
    return "jarvis" if (s.get("track") and not s.get("stale")) else "app"


async def run(action: str = "", level: int | None = None,
              where: str = "auto") -> str:
    if action not in ACTIONS:
        return (f"error: action must be one of {', '.join(ACTIONS)} "
                f"(it is 'prev' not 'previous')")
    dest = _resolve_where(where)

    if action == "volume" and level is None:
        return "error: volume needs a level 0-100"

    if dest == "app" and action in IN_PAGE_ONLY:
        return (f"the music app has no {action} control — it only does pause, "
                f"resume, next and prev. The Jarvis player does; move it there "
                f"with music_play where='jarvis' if they want {action}.")

    if dest == "jarvis":
        fields = {}
        if action == "volume":
            fields["level"] = max(0, min(int(level), 100))
        # The tab that is actually playing, not every open tab — the same
        # addressing music_play uses. Pausing everywhere was less obviously
        # wrong than playing everywhere, but it is the same mistake: it would
        # stop a track on another machine that nobody asked about.
        playing = gui.player_status().get("tab") or None
        target, where_name = gui.resolve_tab(None, playing or runtime.gui_tab.get())
        if target is None:
            return (f"{where_name}, so there is no in-page player to control. "
                    f"Pass where='app' for the music app.")
        n = gui.player_push(action, tab=target, **fields)
        if not n:
            return f"'{where_name}' closed, so there was nothing to control."
        if action == "volume":
            return f"set the Jarvis player on {where_name} to {fields['level']}%."
        return f"{SAID[action]} in the Jarvis player on {where_name}."

    try:
        r = await tarmac.remote(action)
    except tarmac.TarmacError as e:
        return f"error: {e}"
    n = r.get("players", 0)
    return f"{SAID[action]} on {n} music-app player{'s' if n != 1 else ''}."
