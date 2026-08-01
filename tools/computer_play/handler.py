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
            # lexical containment only; the client checks the real file against
            # its own roots, because only the client can see that disk. Scoped
            # to the target machine: a folder granted to the Mac is not a folder
            # on the Linux box, and checking against the union of both said yes
            # to a path that machine could never open.
            params["path"] = await cu.path_within_grants(path, client or None)
            title = Path(params["path"]).stem
        except cu.VerbError as e:
            return f"error: {e}"
    else:
        # search the operator's machine, not this host
        hits, note = [], ""
        if source in ("auto", "local"):
            try:
                r = await cu.dispatch(
                    "find", {"query": query, "kind": kind,
                             "limit": MAX_SHOWN + 1}, client or None, timeout=30)
                res = (r.get("result") or {}) if r.get("ok") else {}
                hits = res.get("hits", [])
                # why there was nothing to search, when that is the answer —
                # "no folder is usable here" is a different fact from "that film
                # is not in the library", and they used to read the same
                note = str(res.get("note") or "")
            except cu.VerbError as e:
                return f"error: {e}"
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
                return (f"{note or f'nothing in the granted folders matches {query!r}'}"
                        f", and {e}")
            except Exception as e:
                return (f"{note or f'nothing local matches {query!r}'}, and "
                        f"Jellyfin could not be reached: {e}")
            if not items:
                if note:
                    return (f"{note}. Nothing in Jellyfin matches '{query}' "
                            f"either.")
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
            return note or f"nothing in the granted folders matches '{query}'."

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
