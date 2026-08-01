"""Pure text helpers for voice mode: sentence chunking for streaming TTS,
markdown-to-speakable sanitizing, and the cut-off bookkeeping that tells the
next turn exactly how much of a reply the operator actually heard.

Everything here is synchronous and side-effect free — unit-tested without a
model, a browser, or the sidecar."""
import re

# --- sentence chunking -------------------------------------------------------

MIN_CHUNK = 25          # don't TTS fragments — prosody needs a real clause
MAX_CHUNK = 250         # force a cut: latency beats perfect sentence shape

_ABBREV = {"mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "inc", "jr",
           "sr", "no", "e.g", "i.e", "approx", "dept", "est", "min", "max"}
_END = ".!?…"


def _cut_ok(buf: str, i: int) -> bool:
    """May we cut after buf[i] (a sentence-end char)? Guards decimals
    ('3.14'), abbreviations ('Dr.'), and requires trailing whitespace so a
    mid-token flush can't split a word."""
    ch = buf[i]
    if i + 1 >= len(buf) or not buf[i + 1].isspace():
        return False                    # need the following whitespace in hand
    if ch == ".":
        if i > 0 and buf[i - 1].isdigit() and i + 2 < len(buf) \
                and buf[i + 2].isdigit():
            return False                # 3.14 — though '.' + space is rare here
        word = re.split(r"[^\w.]", buf[:i])[-1].lower().rstrip(".")
        if word in _ABBREV or len(word) == 1:   # "Dr", initials "J."
            return False
    return True


def chunk_sentences(buf: str) -> tuple[list[str], str]:
    """Split off complete speakable sentences; return (chunks, remainder).
    Newlines always end a chunk (markdown is line-structured). A buffer past
    MAX_CHUNK with no boundary gets cut at its last space."""
    out: list[str] = []
    while buf:
        cut = None
        for i, ch in enumerate(buf):
            if ch == "\n":
                cut = i + 1
                break
            if ch in _END and i + 1 >= MIN_CHUNK and _cut_ok(buf, i):
                cut = i + 1
                break
        if cut is None:
            if len(buf) > MAX_CHUNK:
                sp = buf.rfind(" ", MIN_CHUNK, MAX_CHUNK)
                cut = sp + 1 if sp > 0 else MAX_CHUNK
            else:
                break
        piece = buf[:cut].strip()
        buf = buf[cut:].lstrip(" ")     # sentence gap noise; \n kept (it cuts)
        if piece:
            out.append(piece)
    return out, buf


# --- markdown → speakable ----------------------------------------------------

_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_URL = re.compile(r"https?://\S+")
_MD_INLINE = re.compile(r"[*_`#>|~]+")
_MD_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_MD_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def tts_sanitize(text: str) -> str:
    """One markdown-ish chunk → something worth speaking. '' = skip it."""
    t = text.strip()
    if _MD_TABLE_ROW.match(t):
        return ""                       # tables are eyes-only
    t = _MD_LIST.sub("", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _MD_URL.sub("a link", t)
    t = _MD_INLINE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


class SpeechChunker:
    """Stateful feed of streamed token text → sanitized speakable sentences.
    Fenced code blocks collapse to one '(code omitted.)' announcement."""

    def __init__(self) -> None:
        self._buf = ""
        self._in_fence = False

    def feed(self, text: str) -> list[str]:
        self._buf += text
        chunks, self._buf = chunk_sentences(self._buf)
        return self._filter(chunks)

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return self._filter([rest] if rest else [])

    def _filter(self, chunks: list[str]) -> list[str]:
        out: list[str] = []
        for c in chunks:
            fences = c.count("```")
            if fences % 2 == 1:         # this line opens or closes a block
                self._in_fence = not self._in_fence
                if self._in_fence:
                    out.append("(code omitted.)")
                continue
            if self._in_fence:
                continue
            s = tts_sanitize(c)
            if s:
                out.append(s)
        return out


# --- cut-off bookkeeping -----------------------------------------------------

CUTOFF_NOTHING = "[operator interrupted before any reply was spoken]"
CUTOFF_MARK = ("[— the operator cut the reply off here; they heard "
               "nothing past this point]")


def spoken_fraction(text: str, played_ms: int, dur_ms: int) -> str:
    """The part of `text` covered by played_ms of its dur_ms of audio,
    snapped back to a word boundary."""
    if dur_ms <= 0 or played_ms <= 0:
        return ""
    if played_ms >= dur_ms:
        return text
    chars = int(len(text) * played_ms / dur_ms)
    sp = text.rfind(" ", 0, chars + 1)
    return text[:sp] if sp > 0 else ""


def annotate_cutoff(spoken: str) -> str:
    """The assistant row a barge-in leaves behind: exactly what was heard,
    then the marker the next turn's context will see."""
    spoken = spoken.strip()
    if not spoken:
        return CUTOFF_NOTHING
    return f"{spoken} {CUTOFF_MARK}"


def heard_upto_note(spoken: str, tail_words: int = 12) -> str:
    """Appended to an already-persisted reply whose playback was cut short."""
    words = spoken.strip().split()
    tail = " ".join(words[-tail_words:])
    return (f"\n\n[voice note: playback was interrupted — the operator heard "
            f'only up to: "…{tail}"]')
