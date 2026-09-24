"""Paste-code login: a logged-in browser session mints a one-time code, a CLI
trades it for a device token.

The operator clicks "Add computer" in Settings and gets one line —
`address=<host:port> code=<code>` — to paste into `jav3 login` on the other
machine. The browser session that minted it IS the authorization, so there is
no separate approve step: the ticket is born approved, and the only thing left
is for exactly one process to present the code before it expires.

That makes the code a bearer credential for its lifetime, so it is built like
one:

- **Entropy.** `token_urlsafe(32)` — 256 bits. Nobody types it (it is pasted),
  so there is no reason to trade entropy for readability. Guessing is not a
  threat model; the throttle below is defence in depth and DoS control.
- **Stored hashed.** Tickets are keyed by sha256(code). The raw code exists in
  the mint response and nowhere on the server afterwards — a heap dump or a
  debug print of this module yields nothing redeemable.
- **Lookup without an oracle.** The dict lookup is on the digest, which an
  attacker cannot steer (they would need preimages), and the hit is re-checked
  with `compare_digest`. A malformed, unknown, expired or already-spent code all
  produce the same `None` — one error to the caller, no way to learn which.
- **Single use, atomically.** `redeem` pops the ticket in synchronous code with
  no await between the lookup and the removal, so on the one event loop two
  concurrent redeems of one code cannot both succeed. Pop-before-mint is
  fail-closed: if minting the token then fails, the code is spent and the
  operator makes a new one — never a double mint.
- **Short-lived.** TTL_SECONDS; expired tickets are swept on every mint/redeem.
- **Bounded.** At most MAX_LIVE outstanding tickets; minting past that drops the
  oldest, so a stuck/looping browser tab cannot grow memory without limit.

State is in memory. A restart forgets every outstanding code, which is right
for a ten-minute thing.

Throttling keys on the TCP peer (`request.client.host`), never on
X-Forwarded-For/CF-Connecting-IP: the server is LAN-first with nothing trusted
in front, so those headers are attacker-chosen and would let one host mint
itself a fresh per-peer budget per request. The per-peer budget is the tight
one; the global budget is loose on purpose so a noisy LAN neighbour cannot
lock the operator out of redeeming a code they just made (see _WRONG_GLOBAL).
"""
from __future__ import annotations

import hashlib
import re
import secrets as _secrets
import time
from dataclasses import dataclass

KIND = "paste"
TTL_SECONDS = 10 * 60
CODE_BYTES = 32                  # token_urlsafe(32) -> 43 chars, 256 bits
MAX_LIVE = 16

# token_urlsafe's alphabet; anything else cannot be a code and is refused
# before hashing, as is anything absurdly long (cheap DoS guard).
_CODE_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")

_WRONG_WINDOW = float(TTL_SECONDS)
_WRONG_PER_PEER = 10             # misses per peer per window before 429
_WRONG_GLOBAL = 500              # all peers; loose so a neighbour can't lock us out
_CALLS_WINDOW = 60.0
_CALLS_PER_PEER = 30
_CALLS_GLOBAL = 600


class TooMany(Exception):
    status = 429

    def __init__(self):
        super().__init__("too many login attempts; try again in a few minutes")


@dataclass
class Ticket:
    digest: str                  # sha256(code) hex — the raw code is never kept
    name: str                    # optional label the operator gave the computer
    by: str                      # username of the session that minted it
    created: float
    expires: float
    kind: str = KIND

    def expired(self, now: float) -> bool:
        return now >= self.expires


_tickets: dict[str, Ticket] = {}
_wrong: dict[str, list[float]] = {}
_calls: dict[str, list[float]] = {}
_last_sweep = 0.0


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _sweep(now: float) -> None:
    for d in [d for d, t in _tickets.items() if t.expired(now)]:
        del _tickets[d]


def mint(name: str = "", by: str = "", now: float | None = None) -> tuple[str, Ticket]:
    """A fresh pre-approved ticket. Returns (raw_code, ticket); the raw code is
    returned once, to the authenticated session that asked, and not stored."""
    now = now or time.time()
    _sweep(now)
    while len(_tickets) >= MAX_LIVE:
        oldest = min(_tickets.values(), key=lambda t: t.created)
        del _tickets[oldest.digest]
    code = _secrets.token_urlsafe(CODE_BYTES)
    t = Ticket(digest=_digest(code), name=(name or "").strip()[:64],
               by=(by or "")[:64], created=now, expires=now + TTL_SECONDS)
    _tickets[t.digest] = t
    return code, t


def redeem(code: str | None, now: float | None = None) -> Ticket | None:
    """Spend a code. The ticket on success, else None — for a malformed,
    unknown, expired or already-redeemed code alike.

    MUST stay synchronous: the single-use guarantee is that nothing can run
    between the lookup and the pop.
    """
    now = now or time.time()
    _sweep(now)
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
        return None
    d = _digest(code)
    t = _tickets.pop(d, None)
    if t is None or not _secrets.compare_digest(t.digest, d) or t.expired(now):
        return None
    return t


def live_count(now: float | None = None) -> int:
    _sweep(now or time.time())
    return len(_tickets)


# --- throttling ---------------------------------------------------------------

def _hits(table: dict, key: str, window: float, now: float) -> list[float]:
    return [h for h in table.get(key, []) if now - h < window]


def _bump(table: dict, key: str, window: float, now: float) -> int:
    hits = _hits(table, key, window, now)
    hits.append(now)
    table[key] = hits
    return len(hits)


def _sweep_throttle(now: float) -> None:
    """Drop keys whose hits have all aged out, at most once a minute."""
    global _last_sweep
    if now - _last_sweep < 60:
        return
    _last_sweep = now
    for table, window in ((_wrong, _WRONG_WINDOW), (_calls, _CALLS_WINDOW)):
        for k in [k for k, v in table.items() if not any(now - h < window for h in v)]:
            del table[k]


def throttle(peer: str, now: float | None = None) -> None:
    """Before every redeem. Raises TooMany. A peer that has spent its miss
    budget is refused before its code is even looked at."""
    now = now or time.time()
    _sweep_throttle(now)
    if (len(_hits(_wrong, "*", _WRONG_WINDOW, now)) >= _WRONG_GLOBAL
            or len(_hits(_wrong, peer, _WRONG_WINDOW, now)) >= _WRONG_PER_PEER):
        raise TooMany()
    if (_bump(_calls, "*", _CALLS_WINDOW, now) > _CALLS_GLOBAL
            or _bump(_calls, peer, _CALLS_WINDOW, now) > _CALLS_PER_PEER):
        raise TooMany()


def note_wrong(peer: str, now: float | None = None) -> None:
    """A miss costs the caller budget; a hit costs nothing."""
    now = now or time.time()
    _bump(_wrong, "*", _WRONG_WINDOW, now)
    _bump(_wrong, peer, _WRONG_WINDOW, now)


def reset_for_tests() -> None:
    global _last_sweep
    _last_sweep = 0.0
    _tickets.clear()
    _wrong.clear()
    _calls.clear()
