"""music_play: play from the operator's library on an open TARMAC player."""
from backend import tarmac

SHOWN = 8


async def run(ids: list | None = None, query: str = "", tag: str = "") -> str:
    picked = []
    label = ""
    if ids:
        try:
            picked = [int(i) for i in ids]
        except (TypeError, ValueError):
            return "error: ids must be whole numbers from music_search"
        label = f"{len(picked)} track{'s' if len(picked) != 1 else ''}"
    elif query or tag:
        try:
            rows = await tarmac.search(query, tag or None, limit=SHOWN + 1)
        except tarmac.TarmacError as e:
            return f"error: {e}"
        if not rows:
            return f"nothing matches '{query}'" + (f" with tag {tag}" if tag else "")
        if len(rows) > 1:
            listing = "\n".join(
                f"  [{t.get('id')}] {t.get('title')}"
                + (f" — {t['artist']}" if t.get("artist") else "")
                for t in rows[:SHOWN])
            return (f"{len(rows)} tracks match — say which (pass its id):\n"
                    f"{listing}")
        picked = [rows[0]["id"]]
        label = " — ".join(x for x in (rows[0].get("title"),
                                      rows[0].get("artist")) if x)
    else:
        return "error: give ids, or a query, or a tag"

    try:
        r = await tarmac.remote("play", picked)
    except tarmac.TarmacError as e:
        return f"error: {e}"
    n = r.get("players", 0)
    return f"playing {label} on {n} open player{'s' if n != 1 else ''}."
