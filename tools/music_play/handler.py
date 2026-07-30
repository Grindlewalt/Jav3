"""music_play: one call — find it anywhere, play it, and check it really started.

The old shape cost two or three turns: search, read the results, then play. The
matching is a string problem, so an algorithm does it (backend/musicpick.py) and
this plays the winner straight away. When it genuinely cannot tell, it hands back
a shortlist — or the whole library if nothing matched at all — so the worst case
is one more turn, not a conversation.
"""
import asyncio

from backend import computeruse as cu, musicpick, tarmac

SHOWN = 12
FULL_LIST = 60


async def _tarmac_candidates(query: str, tag: str) -> list:
    try:
        rows = await tarmac.search(query, tag or None, limit=60)
    except tarmac.TarmacError:
        return []
    return [musicpick.Candidate(
        source="tarmac", ref=str(t.get("id")), title=t.get("title") or "",
        artist=t.get("artist") or "", album=t.get("album") or "",
        extra={"tag": t.get("tag")}) for t in rows if t.get("id") is not None]


async def _local_candidates(query: str, client: str) -> list:
    """Granted folders on a connected computer. Skipped silently when none is
    connected — this tool should still work with only the library."""
    if not cu.clients():
        return []
    try:
        r = await cu.dispatch("find", {"query": query, "kind": "audio",
                                       "limit": 60}, client or None, timeout=25)
    except cu.VerbError:
        return []
    hits = (r.get("result") or {}).get("hits", []) if r.get("ok") else []
    out = []
    for path in hits:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        out.append(musicpick.Candidate(source="local", ref=path, title=stem))
    return out


async def _library_listing() -> str:
    """Everything, for when nothing matched — so the model can pick without
    another search."""
    try:
        rows = await tarmac.search("", None, limit=FULL_LIST)
    except tarmac.TarmacError as e:
        return f"(could not list the library either: {e})"
    if not rows:
        return "(the library is empty)"
    lines = [f"  [{t.get('id')}] " + " ".join(
        x for x in (t.get("title"), f"— {t['artist']}" if t.get("artist") else "") if x)
        for t in rows[:FULL_LIST]]
    tail = "" if len(rows) < FULL_LIST else "\n  (first %d shown)" % FULL_LIST
    return "\n".join(lines) + tail


async def _confirm_started(track_id: str, tries: int = 5) -> bool | None:
    """Did sound actually begin?

    TARMAC returns success for the broadcast, not for the audio. Its player calls
    audio.play(), which a browser refuses in a tab that has had no user gesture —
    so "ok" can mean "nothing is audible". The player reports state on its own
    play event, so a moment of polling distinguishes the two. None means we could
    not tell.
    """
    for _ in range(tries):
        await asyncio.sleep(0.6)
        try:
            s = await tarmac.status()
        except tarmac.TarmacError:
            return None
        np = s.get("now_playing") or {}
        if str(np.get("track_id") or np.get("id") or "") == str(track_id):
            return not np.get("paused", False)
    return False


async def run(query: str = "", ids: list | None = None, tag: str = "",
              device: str = "", volume: int | None = None,
              client: str = "") -> str:
    # explicit ids skip matching entirely
    if ids:
        try:
            picked = [int(i) for i in ids]
        except (TypeError, ValueError):
            return "error: ids must be whole numbers"
        try:
            r = await tarmac.remote("play", picked)
        except tarmac.TarmacError as e:
            return f"error: {e}"
        started = await _confirm_started(picked[0])
        return _playing_line(f"{len(picked)} track(s)", r, started)

    if not query and not tag:
        return "error: say what to play, or give ids"

    cands = []
    for group in await asyncio.gather(_tarmac_candidates(query, tag),
                                      _local_candidates(query, client)):
        cands.extend(group)

    if not cands and not query:
        return "nothing in the library carries that tag."
    win, shortlist, why = musicpick.choose(query or tag, cands)

    if win is None:
        if why == "nothing matched":
            listing = await _library_listing()
            return (f"'{query}' is not in the library or the granted folders.\n"
                    f"Everything available:\n{listing}\n\n"
                    f"Play the closest of these by passing its id, or tell the "
                    f"operator it is not available.")
        lines = "\n".join(f"  {musicpick.describe(c)}"
                          + (f"  id={c.ref}" if c.source == "tarmac" else "")
                          for c in shortlist[:SHOWN])
        return (f"{why} for '{query}' — pick one:\n{lines}")

    # a local file goes through the computer, where a device and a level exist
    if win.source == "local":
        params = {"kind": "audio", "path": win.ref, "title": win.title[:300]}
        if device:
            params["device"] = device
        if volume is not None:
            params["volume"] = volume
        try:
            r = await cu.dispatch("play", params, client or None, timeout=25)
        except cu.VerbError as e:
            return f"error: {e}"
        if not r.get("ok"):
            return f"error: {r.get('error')}"
        extra = "".join([f" on {device}" if device else "",
                         f" at {volume}%" if volume is not None else ""])
        return f"playing {win.title} from disk{extra}."

    try:
        r = await tarmac.remote("play", [int(win.ref)])
    except (tarmac.TarmacError, ValueError) as e:
        return f"error: {e}"
    started = await _confirm_started(win.ref)
    note = ""
    if device or volume is not None:
        note = (" (the library plays through the music app, which Jarvis cannot "
                "set an output or a level on — ask for it from a granted folder "
                "if you need that)")
    return _playing_line(musicpick.describe(win), r, started) + note


def _playing_line(what: str, r: dict, started: bool | None) -> str:
    n = r.get("players", 0)
    if started:
        return f"playing {what} on {n} player(s)."
    if started is None:
        return (f"sent {what} to {n} player(s), but could not confirm it "
                f"started.")
    return (f"{what} was accepted by {n} player(s) but no sound has started. "
            f"Browsers block audio in a tab that has not been touched yet — the "
            f"operator needs to press play once in the music app, after which "
            f"remote playback works for the rest of that session.")
