"""computer_play: put something on, from a granted folder or Jellyfin.

Resolution happens here rather than on the client so the client stays dumb: it
receives an absolute path it then re-checks against its own roots, or a stream
URL. Either way it is handed a concrete thing, never a search term.
"""
from pathlib import Path

from backend import computeruse as cu

MAX_SHOWN = 8


async def run(query: str = "", path: str = "", kind: str = "audio",
              source: str = "auto", screen: int | None = None,
              device: str = "", volume: int | None = None,
              client: str = "") -> str:
    if kind not in ("audio", "video"):
        return "error: kind must be 'audio' or 'video'"
    if not query and not path:
        return "error: give a query (what to play) or an exact path"

    params: dict = {"kind": kind}
    title = ""

    if path:
        try:
            params["path"] = await cu.resolve_local(path, kind)
            title = Path(params["path"]).stem
        except cu.VerbError as e:
            return f"error: {e}"
    else:
        # granted folders first — local files are free and always available
        hits = []
        if source in ("auto", "local"):
            hits = await cu.search_local(query, kind, limit=MAX_SHOWN + 1)
        if len(hits) > 1:
            listing = "\n".join(f"  {h}" for h in hits[:MAX_SHOWN])
            more = "" if len(hits) <= MAX_SHOWN else f"\n  ... and more"
            return (f"{len(hits)} files match '{query}' — say which one "
                    f"(pass its path):\n{listing}{more}")
        if hits:
            params["path"] = hits[0]
            title = Path(hits[0]).stem
        elif source in ("auto", "jellyfin"):
            try:
                items = await cu.jellyfin_find(query, kind, limit=MAX_SHOWN + 1)
            except cu.VerbError as e:
                return (f"nothing in the granted folders matches '{query}', "
                        f"and {e}")
            except Exception as e:
                return (f"nothing local matches '{query}', and Jellyfin could "
                        f"not be reached: {e}")
            if not items:
                return f"nothing matches '{query}' in the granted folders or Jellyfin."
            if len(items) > 1:
                listing = "\n".join(
                    f"  {i['name']}" + (f" — {i['artist']}" if i['artist'] else "")
                    for i in items[:MAX_SHOWN])
                return (f"{len(items)} Jellyfin items match '{query}' — say "
                        f"which:\n{listing}")
            params["url"] = await cu.jellyfin_stream_url(items[0]["id"], kind)
            title = items[0]["name"]
        else:
            return f"nothing in the granted folders matches '{query}'."

    if title:
        params["title"] = title[:300]
    if screen is not None:
        params["screen"] = screen
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

    where = []
    if screen is not None:
        where.append(f"screen {screen}")
    if device:
        where.append(device)
    tail = (" on " + " via ".join(where)) if where else ""
    lvl = f" at {volume}%" if volume is not None else ""
    return f"playing {title or 'it'}{tail}{lvl}."
