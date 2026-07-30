"""music_search: find tracks in the operator's library."""
from backend import tarmac

SHOWN = 25


async def run(query: str = "", tag: str = "", limit: int = 25) -> str:
    try:
        rows = await tarmac.search(query, tag or None, limit)
    except tarmac.TarmacError as e:
        return f"error: {e}"
    if not rows:
        what = f"'{query}'" if query else "that"
        return f"nothing in the library matches {what}" + (f" with tag {tag}" if tag else "")
    out = []
    for t in rows[:SHOWN]:
        bits = [f"[{t.get('id')}]", t.get("title") or "(untitled)"]
        if t.get("artist"):
            bits.append(f"— {t['artist']}")
        if t.get("album"):
            bits.append(f"({t['album']})")
        if t.get("tag"):
            bits.append(f"#{t['tag']}")
        out.append("  " + " ".join(bits))
    head = f"{len(rows)} match{'es' if len(rows) != 1 else ''}"
    tail = "" if len(rows) <= SHOWN else f"\n  ... and {len(rows) - SHOWN} more"
    return f"{head}:\n" + "\n".join(out) + tail
