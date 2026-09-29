"""Which files in a project were written by a turn that had read untrusted content.

run_code and read_file execute INSIDE the guest with no broker hop, so the host
never sees what they return. A file a tainted turn wrote (a downloaded README, a
saved news page, a clone) is attacker-authorable text like the page it came from;
a later turn that reads it must not get it as clean input. The write chokepoint
(`workspace_xfer.apply_guest_writes`) records those paths here, the turn spec
ships the list to the guest, and the guest reports a read of one with
`taint_note` (`agent/tools/taintcheck.py`), which taints the turn like a
web_read.

A plain JSON file per project under the DATA dir, next to nothing the guest can
reach: the project directory is what the guest gets a copy of. A clean turn
that overwrites a path makes it clean again; nothing else does. Deleting a file
leaves a stale entry, which costs a spurious taint if a new file ever takes
the name and is then read before a clean turn rewrites it.
"""
import json
import os
import re
from datetime import datetime, timezone

from .config import settings

MAX_STORED = 5000          # entries kept per project; the oldest go first
MAX_LISTED = 2000          # entries shipped in a turn spec
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$", re.I)


def _file(slug):
    if not isinstance(slug, str) or not _SLUG.match(slug) or ".." in slug:
        return None
    return settings.data_dir / "taintpaths" / f"{slug}.json"


def _load(slug) -> dict:
    f = _file(slug)
    if f is None:
        return {}
    try:
        data = json.loads(f.read_text())
        paths = data.get("paths") if isinstance(data, dict) else None
        return paths if isinstance(paths, dict) else {}
    except (OSError, ValueError):
        return {}


def paths(slug) -> list[str]:
    """The tainted paths, oldest first, at most MAX_LISTED (the newest)."""
    return list(_load(slug))[-MAX_LISTED:]


def record(slug, rels, tainted: bool) -> None:
    """A write of `rels` landed. From a tainted turn they join the ledger; from
    a clean one they leave it."""
    f = _file(slug)
    if f is None or not rels:
        return
    cur = _load(slug)
    if not tainted and not cur:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for rel in rels:
        cur.pop(rel, None)                 # re-adding moves it to the newest end
        if tainted:
            cur[rel] = now
    while len(cur) > MAX_STORED:
        cur.pop(next(iter(cur)))
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"paths": cur}))
    os.replace(tmp, f)


def clear(slug) -> None:
    """The operator has looked at the project's files and vouches for them."""
    f = _file(slug)
    if f is not None:
        f.unlink(missing_ok=True)
