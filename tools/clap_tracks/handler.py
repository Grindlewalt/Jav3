"""clap_tracks: edit the double-clap song list in one call.

The list lives in session_state (voice.get_clap_tracks) and the voice session
reads it fresh on every clap, so an edit here is live immediately — no
restart. Removal matching is forgiving (case and spacing dropped — the
musicpick lesson: "kick start my heart" must hit "Kickstart My Heart"). Adds
are verified against the library as an advisory, never a refusal, because
files in granted folders play too.
"""
from backend import tarmac
from backend.db import get_db
from backend.voice import get_clap_tracks, set_clap_tracks


def _norm(s: str) -> str:
    return "".join(str(s).lower().split())


def _matches(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return bool(na) and bool(nb) and (na in nb or nb in na)


async def _library_check(titles: list[str]) -> list[str]:
    """Titles the library could NOT confirm. Empty on any library trouble —
    an unreachable music server must not make every add look like a typo."""
    missing = []
    for t in titles:
        try:
            rows = await tarmac.search(t, None, limit=5)
        except tarmac.TarmacError:
            return []
        if not any(_matches(t, r.get("title") or "") for r in rows):
            missing.append(t)
    return missing


async def run(add: list | None = None, remove: list | None = None) -> str:
    add = [str(t).strip() for t in (add or []) if str(t).strip()]
    remove = [str(t).strip() for t in (remove or []) if str(t).strip()]

    db = await get_db()
    try:
        tracks = await get_clap_tracks(db)

        dropped, unmatched = [], []
        for r in remove:
            hits = [t for t in tracks if _matches(r, t)]
            if hits:
                dropped.extend(hits)
                tracks = [t for t in tracks if t not in hits]
            else:
                unmatched.append(r)

        added, dupes = [], []
        for a in add:
            if any(_norm(a) == _norm(t) for t in tracks):
                dupes.append(a)
            else:
                tracks.append(a)
                added.append(a)

        if added or dropped:
            await set_clap_tracks(db, tracks)
    finally:
        await db.close()

    lines = []
    if added:
        lines.append("added: " + ", ".join(added))
    if dropped:
        lines.append("removed: " + ", ".join(dropped))
    if dupes:
        lines.append("already on the list: " + ", ".join(dupes))
    if unmatched:
        lines.append("no match on the list for: " + ", ".join(unmatched))
    missing = await _library_check(added)
    if missing:
        lines.append("heads up — the library couldn't confirm: "
                     + ", ".join(missing)
                     + " (still added; a granted-folder file would play, "
                     "but a typo won't)")
    if tracks:
        lines.append("the double-clap list is now: " + "; ".join(tracks))
    else:
        lines.append("the double-clap list is now EMPTY — the gesture does "
                     "nothing until a song is added back.")
    return "\n".join(lines)
