"""Pick the track someone meant, without asking a model.

The point is cost and latency. "play kick start my heart" should be one tool
call: an algorithm ranks whatever the sources returned and either plays the
obvious winner or hands back a short list. Sending candidates to an LLM to
choose costs a whole extra turn to answer a question that string comparison
answers correctly.

Deterministic and pure — no I/O, no clock, no randomness. The same query against
the same library always gives the same answer, which is what makes it safe to
act on without confirmation.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

# Noise words that stop a title matching itself: "Kick Start My Heart
# (Remastered 2021) [Official Audio]" should match "kick start my heart".
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_NOISE = re.compile(
    r"\b(official|audio|video|lyric|lyrics|hd|hq|remaster(ed)?|remastered"
    r"|explicit|clean|radio\s*edit|extended|full\s*album|mv|feat|ft)\b")
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

# Leading track numbers on ripped files: "03 - Dr. Feelgood.mp3"
_TRACKNO = re.compile(r"^\s*\d{1,3}\s*[-._)]\s*")


def normalise(text: str) -> str:
    """Fold a title or filename to comparable words."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _TRACKNO.sub("", s)
    s = _BRACKETS.sub(" ", s)
    s = s.replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    s = _NOISE.sub(" ", s)
    return _WS.sub(" ", s).strip()


@dataclass
class Candidate:
    """One playable thing, from whichever source."""
    source: str                 # "tarmac" | "local" | "jellyfin"
    ref: str                    # track id, absolute path, or item id
    title: str
    artist: str = ""
    album: str = ""
    extra: dict = field(default_factory=dict)

    def haystacks(self) -> tuple[str, str]:
        """(title-ish, everything) — scored separately, because a query is far
        more often a title than an album."""
        return (normalise(self.title),
                normalise(" ".join(x for x in (self.title, self.artist,
                                               self.album) if x)))


# Scores are bands, not a continuum, so the margin test below is meaningful.
EXACT_TITLE = 1000
ALL_WORDS_IN_TITLE = 700
TITLE_STARTS = 650
ALL_WORDS_ANYWHERE = 450
FUZZY_BASE = 0

# Below this nothing is confident enough to just play.
MIN_CONFIDENT = 300
# A winner must beat the runner-up by this much, or it is ambiguous and the
# operator gets asked. Two different recordings of the same song score alike,
# and picking one silently is how the wrong one gets played.
MARGIN = 80


def _squash(s: str) -> str:
    """Drop word boundaries entirely.

    "kick start my heart" and "kickstart my heart" are the same request, and a
    word-based comparison scores them as barely related: two of four query words
    are absent from the title. Comparing without spaces makes them identical,
    which is the single most common near-miss in music titles — along with
    "sk8er boi", "ac dc", and every band that cannot agree with itself about
    where a space goes.
    """
    return s.replace(" ", "")


def score(query: str, cand: Candidate) -> int:
    q = normalise(query)
    if not q:
        return 0
    title, everything = cand.haystacks()
    if not everything:
        return 0
    qwords = q.split()
    qs, ts, es = _squash(q), _squash(title), _squash(everything)

    if q == title or qs == ts:
        return EXACT_TITLE
    if ts.startswith(qs) or qs.startswith(ts):
        base = TITLE_STARTS
    elif qs in ts or all(w in title.split() for w in qwords):
        base = ALL_WORDS_IN_TITLE
    elif qs in es or all(w in everything.split() for w in qwords):
        base = ALL_WORDS_ANYWHERE
    else:
        # partial: how much of the query is there, plus overall similarity.
        # The ratio is taken on the squashed forms for the same reason as above.
        present = sum(1 for w in qwords if w in everything)
        ratio = difflib.SequenceMatcher(None, qs, ts).ratio()
        return int(FUZZY_BASE + 200 * (present / len(qwords)) + 150 * ratio)

    # inside a band, prefer the tighter title
    tightness = 1.0 - min(1.0, abs(len(ts) - len(qs)) / max(len(qs), 1))
    return int(base + 60 * tightness)


def rank(query: str, candidates: list[Candidate]) -> list[tuple[int, Candidate]]:
    scored = [(score(query, c), c) for c in candidates]
    # sort by score, then by source so ties are stable rather than dict-ordered
    scored.sort(key=lambda sc: (-sc[0], sc[1].source, sc[1].title.lower()))
    return scored


def choose(query: str, candidates: list[Candidate]
           ) -> tuple[Candidate | None, list[Candidate], str]:
    """(winner, shortlist, why).

    winner is set only when one candidate is both confident and clearly ahead.
    Otherwise the shortlist is what to show, and `why` says which it is — so the
    caller never has to guess whether it may act.
    """
    if not candidates:
        return None, [], "nothing matched"
    scored = rank(query, candidates)
    top_score, top = scored[0]
    if top_score < MIN_CONFIDENT:
        return None, [c for _, c in scored[:8]], "no confident match"
    runner = scored[1][0] if len(scored) > 1 else 0
    if top_score - runner < MARGIN:
        tied = [c for s, c in scored if top_score - s < MARGIN][:8]
        if len(tied) > 1:
            return None, tied, "several equally good matches"
    return top, [c for _, c in scored[:8]], "confident"


def describe(cand: Candidate) -> str:
    bits = [cand.title or "(untitled)"]
    if cand.artist:
        bits.append(f"— {cand.artist}")
    if cand.album:
        bits.append(f"({cand.album})")
    where = {"tarmac": "library", "local": "on disk", "jellyfin": "jellyfin"}
    bits.append(f"[{where.get(cand.source, cand.source)}]")
    return " ".join(bits)
