"""music_play: one call — find it anywhere, play it, and check it really started.

The old shape cost two or three turns: search, read the results, then play. The
matching is a string problem, so an algorithm does it (backend/musicpick.py) and
this plays the winner straight away. When it genuinely cannot tell, it hands back
a shortlist — or the whole library if nothing matched at all — so the worst case
is one more turn, not a conversation.

Three destinations, and they are not interchangeable:

  jarvis  the player inside the Jarvis tab. The host proxies the audio, so this
          is the one that reliably makes sound — the operator is already
          touching that tab, which is the gesture a browser demands before it
          will start audio. Volume and output selection are real here.
  app     TARMAC's own PWA players. What the operator listens on when Jarvis is
          not open, but silent in a tab nobody has touched.
  local   a file in a granted folder, through the desktop client's mpv. The only
          destination with true system audio-device selection.
"""
import asyncio

from backend import computeruse as cu, gui, musicpick, runtime, tarmac

SHOWN = 12
FULL_LIST = 60
MAX_QUEUE = 25          # a queue the model asked for, not the whole library


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


# --- destinations -------------------------------------------------------------

def _resolve_where(where: str) -> str:
    """Which player. `auto` prefers the in-page one whenever a Jarvis tab is
    open, because that is the destination that actually produces sound; with no
    tab open there is nothing to prefer and it falls back to the music app."""
    w = (where or "auto").strip().lower()
    if w in ("jarvis", "page", "here", "browser"):
        return "jarvis"
    if w in ("app", "tarmac", "pwa", "phone"):
        return "app"
    return "jarvis" if gui.tabs() else "app"


async def _queue_rows(ids: list[int]) -> list[dict]:
    """Metadata plus a same-origin stream url per track. The in-page player
    needs the duration for its scrubber, which a caller working from bare ids
    would not have."""
    rows = []
    for i in ids[:MAX_QUEUE]:
        try:
            t = await tarmac.track(i)
        except tarmac.TarmacError:
            continue
        tid = t.get("id")
        if tid is None:
            continue
        rows.append({"id": tid, "title": t.get("title") or f"track {tid}",
                     "artist": t.get("artist") or "", "album": t.get("album") or "",
                     "duration": t.get("duration"), "src": gui.stream_url(tid)})
    return rows


async def _confirm_started(track_id: str, tries: int = 5) -> bool | None:
    """Did sound actually begin on a TARMAC player?

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


async def _confirm_in_page(track_id, tab=None, tries: int = 8) -> tuple[bool, str]:
    """The same question for the in-page player, answered from the tab's own
    reports. Returns (started, error) — the error is whatever the browser said
    when it refused, which is worth passing on verbatim."""
    for _ in range(tries):
        await asyncio.sleep(0.5)
        s = gui.player_status()
        track = s.get("track") or {}
        # a different tab still playing something from earlier must not be read
        # as this request having started
        if tab and s.get("tab") and s["tab"] != tab:
            continue
        if str(track.get("id") or "") != str(track_id):
            continue
        if s.get("error"):
            return False, str(s["error"])
        if s.get("started"):
            return not s.get("paused", False), ""
    return False, ""


async def _play_in_page(ids: list[int], what: str, device: str,
                        volume: int | None, tab: str = "",
                        append: bool = False) -> str:
    rows = await _queue_rows(ids)
    if not rows:
        return ("error: the music server would not describe those tracks, so "
                "there is nothing to stream")
    # ONE tab. This used to go to every open Jarvis tab, so asking for a song
    # started it on the laptop, the desktop and the phone at once — the
    # operator's report. The tab that asked wins; gui.resolve_tab explains the
    # rest of the order.
    target, where = gui.resolve_tab(tab or None, runtime.gui_tab.get())
    if target is None:
        return (f"{where}, so there is no in-page player to play on. Ask the "
                f"operator to open Jarvis, or pass where='app' to send it to "
                f"the music app instead.")
    if append:
        # behind the current track, never interrupting it — the player treats
        # an append onto an empty queue as an ordinary play
        n = gui.player_push("queue_add", tab=target, queue=rows)
        if not n:
            return f"'{where}' closed before it could queue."
        return (f"queued {what} in the Jarvis player on {where} "
                f"({len(rows)} track(s) added).")
    fields: dict = {"queue": rows, "index": 0}
    if volume is not None:
        fields["volume"] = max(0, min(int(volume), 100))
    if device:
        # the tab resolves this against its OWN enumerated outputs, exactly as
        # the desktop client does — a name from the model never becomes an id
        fields["output"] = device
    n = gui.player_push("play", tab=target, **fields)
    if not n:
        return f"'{where}' closed before it could play."
    started, err = await _confirm_in_page(rows[0]["id"], target)
    queued = f" ({len(rows)} queued)" if len(rows) > 1 else ""
    extra = "".join([f", output {device}" if device else "",
                     f", volume {volume}%" if volume is not None else ""])
    if started:
        return f"playing {what} in the Jarvis player on {where}{queued}{extra}."
    if err:
        return (f"{what} was loaded into the Jarvis player on {where} but the "
                f"browser refused to start it: {err}. The operator can press "
                f"play in the player.")
    return (f"{what} was sent to the Jarvis player on {where}{queued} but no "
            f"sound has been confirmed. The player is on screen — the operator "
            f"may need to press play once.")


async def _play_ids(ids: list[int], where: str, what: str, device: str,
                    volume: int | None, tab: str = "",
                    append: bool = False) -> str:
    if where == "jarvis":
        return await _play_in_page(ids, what, device, volume, tab, append)
    if append:
        return ("error: only the Jarvis player has a queue — the music app "
                "can't append. Pass where='jarvis' to queue tracks.")
    try:
        r = await tarmac.remote("play", ids)
    except tarmac.TarmacError as e:
        return f"error: {e}"
    started = await _confirm_started(ids[0])
    note = ""
    if device or volume is not None:
        note = (" (the music app has no output or volume control — the Jarvis "
                "player does, so pass where='jarvis' if they want that)")
    return _playing_line(what, r, started) + note


async def run(query: str = "", ids: list | None = None, tag: str = "",
              device: str = "", volume: int | None = None,
              client: str = "", where: str = "auto", tab: str = "",
              queue: bool = False) -> str:
    dest = _resolve_where(where)

    # explicit ids skip matching entirely
    if ids:
        try:
            picked = [int(i) for i in ids]
        except (TypeError, ValueError):
            return "error: ids must be whole numbers"
        return await _play_ids(picked, dest, f"{len(picked)} track(s)",
                               device, volume, tab, append=queue)

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

    # a local file goes through the computer, where a real audio device exists
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
        track_id = int(win.ref)
    except ValueError:
        return f"error: the library gave a track id that is not a number: {win.ref}"
    return await _play_ids([track_id], dest, musicpick.describe(win),
                           device, volume, tab, append=queue)


def _playing_line(what: str, r: dict, started: bool | None) -> str:
    n = r.get("players", 0)
    if started:
        return f"playing {what} on {n} music-app player(s)."
    if started is None:
        return (f"sent {what} to {n} music-app player(s), but could not confirm "
                f"it started.")
    return (f"{what} was accepted by {n} music-app player(s) but no sound has "
            f"started. Browsers block audio in a tab that has not been touched "
            f"yet — the operator can press play once in the music app, or pass "
            f"where='jarvis' to use the player inside Jarvis, which does not "
            f"have that problem.")
