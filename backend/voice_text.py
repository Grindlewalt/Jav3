"""Pure text helpers for voice mode: sentence chunking for streaming TTS,
markdown-to-speakable sanitizing, the voice-turn prompt block, and the
cut-off bookkeeping that tells the next turn exactly how much of a reply the
operator actually heard.

Everything here is synchronous and side-effect free — unit-tested without a
model, a browser, or the sidecar."""
import re

# Appended to the system prompt of voice turns (chat._run_chat_turn,
# voice=True). Two jobs: the operator must never sit in unannounced silence
# while tools run, and the output must be worth SPEAKING — the TTS layer
# strips markdown and skips tables/code, so producing them is wasted tokens.
VOICE_PROMPT = """\
# Voice mode — you are SPEAKING aloud (TTS); the operator is listening, not reading
- Narrate ONCE, then act: before the FIRST tool call, one short clause \
("Checking the logs.") — never dead silence into tools. But say each thing \
once only: don't restate the request back, don't announce a step AND then \
recap that you did it, don't describe results the operator just heard \
happen. After the tools, go straight to the outcome; skip it entirely when \
the action already speaks for itself (music starting IS the confirmation).
- Talk like a person: short sentences, plain words, contractions. No \
markdown, no headings, no bullet lists, no tables, no code blocks — the \
speech layer strips or skips them. No URLs; name the source in words instead.
- Say numbers and units the way you'd speak them.
- Your FIRST sentence starts being spoken aloud while the rest of the reply \
is still being written, so make it SHORT — six words or fewer, ending in a \
period. Every detail belongs in the sentences after it.
- Keep answers tight. Lead with the answer, then only the detail that \
earns its airtime; offer depth rather than dumping it."""

# Escalation protocol between the local fast model and DeepSeek. The local
# model never silently attempts work above its weight — it emits exactly this
# marker as its whole reply and the orchestrator asks the operator out loud.
ESCALATE_PREFIX = "[ESCALATE]"

# How much of each tool's Notes body the local tier gets. The first line is the
# operating instruction that makes the tool work at all; the rest is written for
# a frontier model doing multi-step work.
LOCAL_NOTES_MAX = 240

# The local tier gets a slim context that drops the behaviour bank, so without
# this it does not know what Jarvis IS — and a model that doesn't know the
# system has a capability says "I can't do that" instead of escalating. This is
# the capability map in the smallest form that still routes correctly.
VOICE_CAPABILITIES = """\
# What Jarvis can do (you are his fast tier, not all of him)
You handle directly: conversation, music and video playback, volume and
playback control on his computers, opening a link, and quick web lookups.
The wider system — which the smart model drives, so ESCALATE rather than
saying it can't be done — also has: reading and writing files in projects,
running code in a sandbox VM, driving the operator's GUI (arranging the
project board, opening dashboards and reports), web research, writing and
scheduling agents that run on their own, memory notes, and his own manual.
If the operator asks for any of that, escalate; never tell him the system
lacks a capability it has."""

# Appended when a voice turn runs on DeepSeek instead of the local tier, so the
# reply can reflect the upgrade the operator paid for (and asked to be able to
# tell apart from the fast tier).
SMART_PROMPT = """\
# You are the SMART model on this turn
The operator escalated to you deliberately — either they named you or they
approved a handoff — so this turn is worth real work: full tool access, more
steps, and an answer the fast tier could not give. Open by making the upgrade
audible in one short clause ("Smart model here." / "On the big model now.")
before anything else, then do the work."""

LOCAL_PROMPT = f"""\
# You are the LOCAL fast model on the operator's own hardware
Conversational replies, media control (play music / video), simple tool \
calls, quick factual questions: handle them yourself — that's your job and \
you're fast at it.
For anything heavy — research, multi-step tool work, writing or analyzing \
code, long documents, anything you might get WRONG — do not attempt it. \
Reply with exactly one line and nothing else:
{ESCALATE_PREFIX} <one short spoken sentence: what you'd hand off and why>
The operator will be asked out loud whether to send it to the smart model."""

# --- wake / standby phrases ---------------------------------------------------

# openwakeword gives a fast "hey jarvis" flip, but it is one fixed model: it
# does not fire on a bare "Jarvis", and whisper cheerfully renders the name as
# Dervis/Jervis/Charvis (all observed live). The sidecar transcribes every
# utterance whether or not the wake model fired, so the authoritative wake test
# is done HERE on the text — the acoustic detector is only an early chime.
_WAKE_NAMES = ("jarvis", "jervis", "dervis", "javis", "charvis", "jarvi",
               "darvis", "garvis")
# Consumed before the name: "hey there Jarvis", "um, ok Jarvis". Bounded at two
# so a real sentence can never be eaten looking for a name that isn't coming.
_WAKE_LEADS = ("hey", "hi", "hello", "ok", "okay", "yo", "there", "um", "uh", "so")
# Politeness the operator adds to a standby command; ignored when matching.
_TRAILING_FILLER = ("please", "now", "thanks", "thank you", "for now", "buddy")

# Said while awake, these put him back on standby without a reply. "Never mind"
# is in here deliberately: mid-request it means stop, and stopping is exactly
# what standby is.
SLEEP_PHRASES = ("go to sleep", "go back to sleep", "back to sleep", "sleep now",
                 "stand down", "stop listening", "stand by", "standby",
                 "that's all", "thats all", "that is all", "never mind",
                 "nevermind", "nothing", "forget it", "dismissed")

# Harder stop: end the session outright (mic released, socket closed).
SHUTDOWN_PHRASES = ("shut down", "shutdown", "shut yourself down", "power down",
                    "power off", "goodbye", "good bye", "log off", "sign off",
                    "end session", "turn off")

_PUNCT = " \t\n.,!?;:—-\"'"


def _norm(text: str) -> str:
    return " ".join(text.lower().strip(_PUNCT).split())


def split_wake(text: str) -> tuple[bool, str]:
    """(wake_heard, remainder).

    "Hey Jarvis, what's my schedule" -> (True, "what's my schedule")
    "Jarvis"                         -> (True, "")
    "what's my schedule"             -> (False, "what's my schedule")

    The remainder keeps the ORIGINAL casing/punctuation — only the wake prefix
    is removed, because the remainder becomes the operator's actual turn.
    """
    flat = _norm(text)
    if not flat:
        return False, ""
    # strip punctuation off each token: whisper writes "Hey Jarvis, what..." and
    # "Okay Jarvis: turn it down", so the name arrives glued to a comma/colon
    words = [w.strip(_PUNCT) for w in flat.split()]
    used = 0
    while used < len(words) and used < 2 and words[used] in _WAKE_LEADS:
        used += 1
    if used < len(words) and words[used] in _WAKE_NAMES:
        used += 1
    else:
        # a lead word alone ("hey") is not a wake; put it back
        return False, text.strip()
    if used >= len(words):
        return True, ""
    # walk the same number of words off the ORIGINAL string
    rest = text.strip().lstrip(_PUNCT)
    for _ in range(used):
        rest = rest.lstrip(_PUNCT)
        sp = rest.find(" ")
        rest = "" if sp < 0 else rest[sp + 1:]
    return True, rest.lstrip(_PUNCT).strip()


def _phrase_hit(text: str, phrases: tuple[str, ...]) -> bool:
    """True when the utterance IS one of these phrases (not merely contains
    one) — "never mind the news, play something" must not put him to sleep."""
    _, flat = split_wake(text)          # "Jarvis, stand down" -> "stand down"
    flat = " ".join(w.strip(_PUNCT) for w in _norm(flat).split())
    for filler in sorted(_TRAILING_FILLER, key=len, reverse=True):
        if flat.endswith(" " + filler):
            flat = flat[: -len(filler) - 1].strip()
            break
    return flat in phrases


def is_sleep_command(text: str) -> bool:
    return _phrase_hit(text, SLEEP_PHRASES)


def is_shutdown_command(text: str) -> bool:
    return _phrase_hit(text, SHUTDOWN_PHRASES)


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


# The opening of a reply gets a fast path: the operator has been waiting
# through STT + model latency already, so the FIRST speakable piece cuts at
# the first clause boundary (comma/colon/semicolon) instead of a full
# sentence. Chunk 1 trades a little prosody for first-audio latency; every
# later chunk uses the normal sentence rules.
FIRST_CUT_MIN = 12
# The cap is a LATENCY knob, not a style one: the synth runs ~6x realtime, so
# first-audio ≈ (spoken length of this piece)/6 + ~180 ms of fixed cost. At 80
# the opening piece could reach ~3 s of speech and blow the budget on its own
# (measured 513 ms of TTS for "There are 5,280 feet in a mile."). Measured
# speech-end→first-audio medians over 8 utterances, with the short-opener rule
# in VOICE_PROMPT: 80 -> 878 ms, 56 -> 846 ms, 40 -> 785 ms (7/8 under the
# 880 ms target). Below 40 it starts cutting mid-phrase for no real gain.
FIRST_CUT_MAX = 40


def first_clause_cut(buf: str) -> int | None:
    """Earliest defensible cut for the first spoken piece, or None to wait."""
    for i, ch in enumerate(buf):
        if ch == "\n" and i >= 1:
            return i + 1
        if i + 1 >= FIRST_CUT_MIN and i + 1 < len(buf) and buf[i + 1].isspace():
            if ch in _END and _cut_ok(buf, i):
                return i + 1
            if ch in ",;:":
                return i + 1
    if len(buf) > FIRST_CUT_MAX:
        sp = buf.rfind(" ", FIRST_CUT_MIN, FIRST_CUT_MAX)
        return sp + 1 if sp > 0 else FIRST_CUT_MAX
    return None


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
        self._spoke = False              # first emission unlocks the fast path

    def feed(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        if not self._spoke:
            # opening fast path: get SOMETHING to the speakers at the first
            # clause boundary rather than the first full sentence
            cut = first_clause_cut(self._buf)
            if cut is not None:
                piece, self._buf = self._buf[:cut].strip(), self._buf[cut:].lstrip(" ")
                out += self._filter([piece] if piece else [])
        chunks, self._buf = chunk_sentences(self._buf)
        out += self._filter(chunks)
        return out

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
                    self._spoke = True
                continue
            if self._in_fence:
                continue
            s = tts_sanitize(c)
            if s:
                out.append(s)
                self._spoke = True
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
