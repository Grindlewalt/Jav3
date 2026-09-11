"""A machine gets its credentials by being confirmed in a browser, not by having
them pasted into a terminal.

The set-up command used to carry everything a machine needs to reach Jarvis —
the pairing token and, behind Cloudflare Access, the service token secret — in
plain text, because the operator had to get them onto the new machine somehow
and a paste was the only way. So the two most sensitive strings in the whole
deployment lived in a clipboard, a shell history, a terminal scrollback and any
screenshot of it, on every machine ever set up. That was the least secure part
of the design, and it was that way only for want of a channel.

This is the channel. It is the device-authorization shape (RFC 8628, the thing
behind `gh auth login`), with the operator starting it rather than the device:

  1. The operator asks the Computer use tab for a **pairing code** — eight
     characters from an alphabet with no 0/O or 1/I, shown as XXXX-XXXX, good
     for fifteen minutes. The set-up command carries the code and nothing else.
  2. The new machine runs the command. The client **claims** the code: it says
     its name, hostname and platform, and gets back a device secret that only
     that process holds, plus the address of a confirm page.
  3. The operator opens that page in a browser where they are logged in to
     Jarvis (or just watches the wizard, which shows the same thing), sees
     WHICH machine claimed the code — name, host, platform, where from — and
     **confirms** or denies it.
  4. The client, polling with its device secret, receives the pairing token and
     the Access service token exactly once, writes them to its 0600 config, and
     carries on with set-up as before.

What that buys. The pasted command holds a code that on its own yields nothing:
claiming it gets a stranger as far as a confirm page the operator has to say
yes to, and the page names the claimant. A code that is claimed twice is
flagged as contested, so the operator sees the race rather than confirming the
wrong side of it. The credentials leave the host only in the reply to a poll
carrying the device secret, which was never displayed anywhere. And the code
is dead after one use or fifteen minutes, whichever is first.

What it does not buy. The machine-side routes (claim, poll, the client download)
have to be reachable by a process that has no credentials yet, so behind
Cloudflare Access they need a Bypass policy on their path — which means they are
reachable by anyone. They are built for that: nothing here returns a credential
without a confirmed ticket and its device secret, an unknown code and an expired
one are indistinguishable from outside, and every unauthenticated call is
throttled below any rate that could enumerate the code space (32^8 codes, sixty
wrong guesses per quarter hour).

State lives in memory. A restart forgets every pending code, which is right: a
code is a fifteen-minute thing, and the client's poll says so and stops.
"""
from __future__ import annotations

import secrets as _secrets
import time
from dataclasses import dataclass, field

# no 0/O, no 1/I — the code is read off one screen and typed on another
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
TTL_SECONDS = 15 * 60
POLL_INTERVAL = 3

# Unauthenticated-call throttling. Two budgets, because the two failure modes
# differ: an attacker guessing codes needs to be stopped far below the code
# space, while a legitimate client polling every 3s for 15 minutes needs ~300
# calls and must not be cut off at 299.
_WRONG_CODE_WINDOW = TTL_SECONDS
_WRONG_CODE_PER_PEER = 10
_WRONG_CODE_GLOBAL = 60
_CALLS_WINDOW = 60.0
_CALLS_PER_PEER = 60
_CALLS_GLOBAL = 900


class PairingError(Exception):
    """Base: the message is fit to show the client or the operator."""
    status = 400


class Unknown(PairingError):
    """No such live code, or the device secret does not match. One error for
    both, so a caller learns nothing about which codes exist."""
    status = 404

    def __init__(self):
        super().__init__("no such pairing code, or it has expired — make a "
                         "new one on the Computer use tab")


class Contested(PairingError):
    """A second machine tried to claim a code that was already claimed."""
    status = 409

    def __init__(self):
        super().__init__("this code was already claimed by another machine. "
                         "The operator will see both attempts; make a new code")


class WrongState(PairingError):
    status = 409


class TooMany(PairingError):
    status = 429

    def __init__(self):
        super().__init__("too many pairing requests; try again in a few minutes")


@dataclass
class Ticket:
    code: str
    name: str                      # what the operator called the machine
    created: float
    expires: float
    # waiting  — code issued, no machine has claimed it
    # claimed  — a machine has it and is polling; needs the operator's yes
    # approved — the operator said yes; credentials not yet collected
    # released — credentials handed over once; the code is spent
    # denied   — the operator said no
    state: str = "waiting"
    device_secret: str = ""
    claim: dict = field(default_factory=dict)
    contested: list = field(default_factory=list)    # later claim attempts
    approved_by: str = ""

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires

    def public(self, now: float | None = None) -> dict:
        """The operator's view. Never the device secret: it is the one thing
        that must exist only in the claiming process."""
        now = now or time.time()
        return {"code": self.code, "name": self.name, "state": self.state,
                "created_at": self.created, "expires_at": self.expires,
                "expires_in": max(0, int(self.expires - now)),
                "claim": dict(self.claim), "contested": list(self.contested),
                "approved_by": self.approved_by}


_tickets: dict[str, Ticket] = {}
_wrong: dict[str, list[float]] = {}
_calls: dict[str, list[float]] = {}


def normalize(code: str | None) -> str | None:
    """Canonical XXXX-XXXX, or None for anything that cannot be a code.

    Dashes and spaces are decoration; case is not information. A code with a
    character outside the alphabet is refused here rather than looked up, so a
    lookup miss always means "not issued or expired" and nothing else.
    """
    raw = "".join(c for c in str(code or "").upper() if c not in "- ")
    if len(raw) != CODE_LEN or any(c not in ALPHABET for c in raw):
        return None
    return f"{raw[:4]}-{raw[4:]}"


def _new_code() -> str:
    while True:
        raw = "".join(_secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        code = f"{raw[:4]}-{raw[4:]}"
        if code not in _tickets:
            return code


def sweep(now: float | None = None) -> None:
    now = now or time.time()
    for code in [c for c, t in _tickets.items() if t.expired(now)]:
        del _tickets[code]


def create(name: str, now: float | None = None) -> Ticket:
    """A fresh code for a machine the operator is about to set up."""
    now = now or time.time()
    sweep(now)
    t = Ticket(code=_new_code(), name=(name or "").strip()[:64],
               created=now, expires=now + TTL_SECONDS)
    _tickets[t.code] = t
    return t


def live(now: float | None = None) -> list[Ticket]:
    sweep(now)
    return sorted(_tickets.values(), key=lambda t: t.created)


def get(code: str | None, now: float | None = None) -> Ticket | None:
    """A live ticket, or None. Expired reads as absent."""
    now = now or time.time()
    key = normalize(code)
    t = _tickets.get(key) if key else None
    if t is None or t.expired(now):
        return None
    return t


def claim(code: str | None, *, name: str = "", hostname: str = "",
          platform: str = "", peer: str = "", agent: str = "",
          now: float | None = None) -> Ticket:
    """The machine side: take the code, get a device secret.

    A second claim does not overwrite the first. The first claimant may be the
    operator's machine and the second an attacker who saw the code — or the
    other way round — and the host cannot tell. So the second is refused AND
    recorded on the ticket, and the confirm page shows both; the operator, who
    knows which machine they are sitting at, is the one who can decide.
    """
    now = now or time.time()
    t = get(code, now)
    if t is None:
        raise Unknown()
    attempt = {"name": (name or "").strip()[:64],
               "hostname": (hostname or "").strip()[:128],
               "platform": (platform or "").strip()[:32],
               "peer": (peer or "")[:64], "agent": (agent or "")[:120],
               "at": now}
    if t.state in ("denied", "released"):
        raise Unknown()              # a spent or refused code looks like no code
    if t.state != "waiting":
        t.contested.append(attempt)
        del t.contested[:-5]
        raise Contested()
    t.state = "claimed"
    t.device_secret = _secrets.token_urlsafe(32)
    t.claim = attempt
    return t


def approve(code: str | None, by: str = "", now: float | None = None) -> Ticket:
    t = get(code, now)
    if t is None:
        raise Unknown()
    if t.state != "claimed":
        raise WrongState(
            "nothing to confirm yet — no machine has claimed this code"
            if t.state == "waiting" else
            f"this code is already {t.state}")
    t.state = "approved"
    t.approved_by = by or ""
    return t


def deny(code: str | None, now: float | None = None) -> Ticket:
    """Also how a code is cancelled before anything claimed it."""
    t = get(code, now)
    if t is None:
        raise Unknown()
    if t.state == "released":
        raise WrongState("the credentials were already handed over; rotate the "
                         "pairing token if that machine should not have them")
    t.state = "denied"
    return t


def poll(code: str | None, device_secret: str | None,
         now: float | None = None) -> Ticket:
    """The machine side: the ticket, if the device secret is the one issued.

    The caller reads `state` and, on "approved", collects the credentials and
    calls release(). A secret mismatch is Unknown, same as a bad code — a poll
    with the wrong secret must not confirm the code exists.
    """
    t = get(code, now)
    if t is None or not t.device_secret:
        raise Unknown()
    if not _secrets.compare_digest(device_secret or "", t.device_secret):
        raise Unknown()
    return t


def release(t: Ticket) -> None:
    """Credentials are leaving in this response. Exactly once."""
    t.state = "released"
    t.device_secret = ""


def forget(code: str | None) -> None:
    key = normalize(code)
    if key:
        _tickets.pop(key, None)


# --- throttling ----------------------------------------------------------------

def _bump(table: dict, key: str, window: float, now: float) -> int:
    hits = [h for h in table.get(key, []) if now - h < window]
    hits.append(now)
    table[key] = hits
    return len(hits)


def _count(table: dict, key: str, window: float, now: float) -> int:
    return len([h for h in table.get(key, []) if now - h < window])


def throttle(peer: str, now: float | None = None) -> None:
    """Called before every unauthenticated pairing route. Raises TooMany.

    The peer is a hint (behind a proxy every request may share one), so the
    global budget is the one that actually bounds an attacker; the per-peer
    budget is there so a single stuck client cannot spend the global one.
    """
    now = now or time.time()
    if (_count(_wrong, "*", _WRONG_CODE_WINDOW, now) >= _WRONG_CODE_GLOBAL
            or _count(_wrong, peer, _WRONG_CODE_WINDOW, now) >= _WRONG_CODE_PER_PEER):
        raise TooMany()
    if (_bump(_calls, "*", _CALLS_WINDOW, now) > _CALLS_GLOBAL
            or _bump(_calls, peer, _CALLS_WINDOW, now) > _CALLS_PER_PEER):
        raise TooMany()


def note_wrong_code(peer: str, now: float | None = None) -> None:
    """A miss costs the guesser budget; a hit costs nothing."""
    now = now or time.time()
    _bump(_wrong, "*", _WRONG_CODE_WINDOW, now)
    _bump(_wrong, peer, _WRONG_CODE_WINDOW, now)


def reset_for_tests() -> None:
    _tickets.clear()
    _wrong.clear()
    _calls.clear()
