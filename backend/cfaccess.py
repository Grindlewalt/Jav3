"""The Cloudflare Access service token, in one place.

Jarvis and the music server sit behind Cloudflare Access. A browser gets an
interactive SSO redirect; a daemon cannot complete one, so everything headless
presents a **service token** — a client id and secret pair — as two headers.

Before this module that pair existed in at least three places at once: the
`tarmac_cf_*` rows in SQLite, whatever the operator had typed into the Computer
use tab, and a copy in every desktop client's own config file. Rotating the
token in Cloudflare therefore broke things one at a time and silently — music
stopped working with a redirect the operator never saw, and the set-up command
kept carrying a secret that no longer existed. That is the failure this exists
to prevent, and it is worth being concrete: a rotation cost an evening of
debugging a "403" that was really a stale copy in a place nobody remembered.

So: one canonical copy, in the 0600 secret store rather than in the database.
That is not tidiness. Living in `secrets.py` means the pair is covered by the
machinery already built around that file — `scrub()` keeps it out of anything
fed back to the model, and `find_in_bytes()` makes `writes.apply_write` refuse
any file the agent tries to write it into. The database has none of that.

Bound to hosts on save, for the same reason every other secret is: an unbound
secret can be substituted into a URL pointing anywhere, and this one opens the
operator's front door.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from . import secrets as secretstore

# Reserved names in the operator's secret store. Uppercase to satisfy the
# store's own name rule, and spelled like the headers they become so that
# finding one in a config file elsewhere is unambiguous.
NAME_ID = "CF_ACCESS_CLIENT_ID"
NAME_SECRET = "CF_ACCESS_CLIENT_SECRET"

HEADER_ID = "CF-Access-Client-Id"
HEADER_SECRET = "CF-Access-Client-Secret"


class CFAccessError(RuntimeError):
    """The service token is missing or malformed."""


def get() -> tuple[str, str]:
    """(client_id, secret). Either may be "" when nothing is configured."""
    store = secretstore.load()
    return (store.get(NAME_ID) or "").strip(), (store.get(NAME_SECRET) or "").strip()


def configured() -> bool:
    cid, sec = get()
    return bool(cid and sec)


def headers() -> dict[str, str]:
    """The two headers, or {} when unconfigured.

    Empty rather than raising: Jarvis is not always behind Access, and a
    deployment that is not should not have to special-case every call site.
    """
    cid, sec = get()
    return {HEADER_ID: cid, HEADER_SECRET: sec} if cid and sec else {}


def hosts() -> list[str]:
    """The hosts this token is bound to — the ones it may be sent to."""
    return secretstore.hosts_for(NAME_SECRET)


def _clean(value: str, what: str) -> str:
    """Service tokens get copied out of a web console, so they arrive with
    stray whitespace, smart quotes and occasionally the header name still
    attached. Strip what is unambiguous and refuse what is not."""
    v = (value or "").strip().strip('"').strip("'").strip()
    # "CF-Access-Client-Id: abc..." pasted whole
    for header in (HEADER_ID, HEADER_SECRET):
        if v.lower().startswith(header.lower() + ":"):
            v = v[len(header) + 1:].strip()
    if not v:
        raise CFAccessError(f"the {what} is empty")
    if any(c.isspace() for c in v):
        raise CFAccessError(
            f"the {what} contains a space — it should be one unbroken string")
    if not all(c.isalnum() or c in "._-" for c in v):
        raise CFAccessError(
            f"the {what} has characters a service token never has; copy it "
            f"again from Zero Trust > Access > Service Auth")
    return v


def set_token(client_id: str, secret: str, bind_hosts: list[str] | None = None) -> None:
    """Save the pair, bound to the hosts it may be presented to.

    A blank `secret` keeps the stored one, so the GUI can re-save the id or
    re-bind hosts without the operator re-typing a secret it never shows them.
    """
    cid = _clean(client_id, "client id")
    if not cid.endswith(".access"):
        # Cloudflare's ids always do. Getting this wrong silently produces a
        # login redirect, which reads as "wrong password" rather than "wrong
        # field", so it is worth catching here.
        raise CFAccessError(
            "a Cloudflare service token client id ends in '.access' — that "
            "looks like the secret, or like something else entirely")
    sec = _clean(secret, "client secret") if secret else get()[1]
    if not sec:
        raise CFAccessError("no secret stored yet, so one has to be given")

    raw = secretstore._load_raw()
    bound = sorted({h.strip().lower() for h in (bind_hosts or hosts()) if h.strip()})
    raw[NAME_ID] = {"value": cid, "hosts": bound}
    raw[NAME_SECRET] = {"value": sec, "hosts": bound}
    secretstore.save(raw)


def bind_host_from_url(url: str) -> str:
    """The hostname a URL will be presented to, for binding."""
    host = (urlsplit(url or "").hostname or "").lower()
    return host


def clear() -> None:
    raw = secretstore._load_raw()
    raw.pop(NAME_ID, None)
    raw.pop(NAME_SECRET, None)
    secretstore.save(raw)
