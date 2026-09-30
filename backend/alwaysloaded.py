"""The files that ride EVERY prompt of a project, and the gate in front of them.

Two things reach a project's system prompt whole, turn after turn: project.md
and the files the operator ticked in the Context panel (projects/<slug>/
.context.json). Text there is the standing instruction an injection wants to
own, and an agent can write both with write_file / edit_file / run_code. So the
list is explicit and the operator's alone:

  * `files(slug)` is the list: project.md (always) plus the ticked files.
    Changing it is the operator's cookie session on PUT /api/projects/<slug>/
    context, and an `always_loaded_changed` security event (info) records it.
    An agent cannot touch it: `.context.json` is never shipped into the guest,
    never taken back out (workspace_xfer.SKIP), and `writes.apply_write` refuses it.
  * A write to a file on the list from a turn that had read untrusted content
    (broker ledger, `taintpaths`) does not land. It is HELD here, on the host
    outside anything the guest can reach, with the file it would replace, and the
    prompt keeps reading the file as it was until the operator approves the
    change on the Memory page (a diff, bound to the text they read) or rejects it.
  * Untainted writes, and writes to files off the list, are exactly as before.

Same shape as the memory-note proposals (memory.py): one held change per file,
the newest wins, approval binds to the sha256 the operator read, an edit made
to the file since the hold needs an explicit "approve anyway".
"""
import base64
import difflib
import hashlib
import json
import os
import posixpath
import re
from datetime import datetime, timezone

from . import runtime, taintpaths
from .config import settings

PROJECT_MD = "project.md"
MAX_HELD_BYTES = 2_000_000      # a bigger file is refused outright, not held
MAX_HELD = 50                   # per project; the oldest go first
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$", re.I)


class HeldChanged(Exception):
    """The held change is not the text the operator was looking at."""


class HeldStale(Exception):
    """The file itself changed after the change was held."""


class HeldTooLarge(ValueError):
    pass


# --- the list ------------------------------------------------------------------

def _norm(rel) -> str:
    """One spelling of a project-relative path: forward slashes, no './', no
    '..' or leading '/'. Case-folded, because a case-insensitive filesystem
    would land 'Project.md' on project.md."""
    s = posixpath.normpath(str(rel).replace("\\", "/").strip()).lstrip("/")
    return "" if s == "." else s.casefold()


def selection(slug: str) -> list[str]:
    """The operator-ticked context files, as stored."""
    from .memory import context_selection
    return [f for f in context_selection(slug) if isinstance(f, str) and f]


def files(slug: str) -> list[str]:
    """Every file loaded whole into the project's prompt: project.md first, then
    the ticked files in their order."""
    out, seen = [PROJECT_MD], {_norm(PROJECT_MD)}
    for f in selection(slug):
        if _norm(f) not in seen:
            seen.add(_norm(f))
            out.append(f)
    return out


def is_loaded(slug: str, rel: str) -> bool:
    """Would a write to `rel` land in a file that rides every prompt? Compared by
    normalized name and, for a file that exists, by identity (a symlink or a
    case-insensitive twin of a listed file is the listed file)."""
    want = _norm(rel)
    if not want:
        return False
    listed = files(slug)
    if any(_norm(f) == want for f in listed):
        return True
    base = settings.projects_dir / slug
    try:
        dest = base / rel
        if not dest.exists():
            return False
        return any((base / f).exists() and os.path.samefile(dest, base / f) for f in listed)
    except (OSError, ValueError):
        return False


def set_selection(slug: str, chosen: list[str]) -> tuple[list[str], list[str]]:
    """Store the operator's ticks (project.md is always loaded and never stored,
    or the prompt would carry it twice). Returns (added, removed) for the audit
    line. Callers are the operator's routes only."""
    from .memory import set_context_selection
    before = selection(slug)
    keep, seen = [], {_norm(PROJECT_MD)}
    for f in chosen:
        if _norm(f) not in seen:
            seen.add(_norm(f))
            keep.append(f)
    set_context_selection(slug, keep)
    b = {_norm(f) for f in before}
    a = {_norm(f) for f in keep}
    return ([f for f in keep if _norm(f) not in b],
            [f for f in before if _norm(f) not in a])


async def record_change(slug: str, added: list[str], removed: list[str]) -> None:
    """One info security event for the operator changing what is always loaded."""
    if not added and not removed:
        return
    bits = ([f"now loaded: {', '.join(added)}"] if added else []) + \
           ([f"no longer loaded: {', '.join(removed)}"] if removed else [])
    await _audit("always_loaded_changed", "info", slug,
                 f"always-loaded files of '{slug}' changed by the operator ({'; '.join(bits)})",
                 {"added": added, "removed": removed, "by": "operator"})


# --- held changes --------------------------------------------------------------

def _dir(slug: str):
    if not isinstance(slug, str) or not _SLUG.match(slug) or ".." in slug:
        return None
    return settings.data_dir / "heldwrites" / slug


def _id(rel: str) -> str:
    return hashlib.sha256(_norm(rel).encode()).hexdigest()[:16]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(slug: str, rel: str) -> bytes | None:
    base = (settings.projects_dir / slug).resolve()
    try:
        p = (base / rel).resolve()
        return p.read_bytes() if p.is_relative_to(base) and p.is_file() else None
    except OSError:
        return None


def _read(slug: str, item_id: str) -> dict | None:
    d = _dir(slug)
    if d is None or not re.fullmatch(r"[0-9a-f]{16}", str(item_id)):
        return None
    try:
        return json.loads((d / f"{item_id}.json").read_text())
    except (OSError, ValueError):
        return None


def _drop(slug: str, item_id: str) -> bool:
    d = _dir(slug)
    if d is None or not re.fullmatch(r"[0-9a-f]{16}", str(item_id)):
        return False
    p = d / f"{item_id}.json"
    if not p.is_file():
        return False
    p.unlink()
    return True


async def hold(slug: str, rel: str, content: bytes) -> dict:
    """Keep `content` as the proposed new text of `rel` instead of writing it.
    Replaces an earlier held change to the same file. The caller has already
    done the path and secret checks (writes._check)."""
    d = _dir(slug)
    if d is None:
        raise ValueError("bad project name")
    if len(content) > MAX_HELD_BYTES:
        await _audit("always_loaded_refused", "warn", slug,
                     f"write to always-loaded file {rel} refused: too large to hold "
                     f"({len(content):,} bytes)", {"path": rel, "bytes": len(content)})
        raise HeldTooLarge(rel)
    base = _canonical(slug, rel)
    item = {"path": rel, "sha256": _sha(content), "size": len(content),
            "content_b64": base64.b64encode(content).decode(),
            "base_exists": base is not None,
            "base_sha256": _sha(base) if base is not None else None,
            "held_at": _now(),
            "conversation_id": runtime.conversation_id.get()}
    d.mkdir(parents=True, exist_ok=True)
    iid = _id(rel)
    tmp = d / f"{iid}.tmp"
    tmp.write_text(json.dumps(item))
    os.replace(tmp, d / f"{iid}.json")
    _trim(d)
    await _audit("always_loaded_held", "warn", slug,
                 f"change to always-loaded file {rel} held for your approval: the turn had "
                 "read untrusted content",
                 {"path": rel, "bytes": len(content), "id": iid}, cause=f"held:{slug}:{_norm(rel)}")
    _notify(slug, rel)
    return _view(slug, iid, item)


def _trim(d) -> None:
    items = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for p in items[:max(0, len(items) - MAX_HELD)]:
        p.unlink(missing_ok=True)


def _view(slug: str, iid: str, item: dict) -> dict:
    try:
        new = base64.b64decode(item.get("content_b64") or "")
    except ValueError:
        new = b""
    cur = _canonical(slug, item["path"])
    want = item.get("base_sha256")
    try:
        new_t, binary = new.decode("utf-8"), False
    except UnicodeDecodeError:
        new_t, binary = new.decode("utf-8", errors="replace"), True
    old_t = (cur or b"").decode("utf-8", errors="replace")
    diff = "".join(difflib.unified_diff(
        (old_t.rstrip("\n") + "\n").splitlines(True) if old_t else [],
        (new_t.rstrip("\n") + "\n").splitlines(True),
        "current", "proposed"))
    return {"project": slug, "id": iid, "path": item["path"],
            "size": item.get("size", len(new)), "binary": binary,
            "sha256": item.get("sha256"), "base_sha256": want,
            "base_exists": cur is not None,
            "stale": (_sha(cur) if cur is not None else None) != want,
            "held_at": item.get("held_at"),
            "conversation_id": item.get("conversation_id"),
            "diff": "" if binary else diff[:20000],
            "body": "" if binary else new_t[:20000]}


def list_held(slug: str | None = None) -> list[dict]:
    """Held changes, oldest first: one project's, or every project's."""
    root = settings.data_dir / "heldwrites"
    dirs = [_dir(slug)] if slug else (sorted(p for p in root.iterdir() if p.is_dir())
                                      if root.is_dir() else [])
    out = []
    for d in dirs:
        if d is None or not d.is_dir():
            continue
        for p in d.glob("*.json"):
            try:
                item = json.loads(p.read_text())
                out.append(_view(d.name, p.stem, item))
            except (OSError, ValueError, KeyError):
                continue
    out.sort(key=lambda v: (v.get("held_at") or "", v["project"], v["path"]))
    return out


def held_paths(slug: str) -> list[str]:
    return [v["path"] for v in list_held(slug)]


def pending_total() -> int:
    """How many held changes wait, all projects: part of the Memory nav badge."""
    root = settings.data_dir / "heldwrites"
    return sum(1 for d in (root.iterdir() if root.is_dir() else ())
               if d.is_dir() for _ in d.glob("*.json"))


async def approve(slug: str, item_id: str, *, sha256: str | None = None,
                  force: bool = False) -> str:
    """Land a held change. The operator read the diff, so the file is vouched
    for: it leaves the tainted-paths ledger. `sha256` binds the approval to the
    exact text they read (HeldChanged if the agent held a newer one since); a
    file that changed after the hold needs `force` (HeldStale). The write goes
    through writes.apply_write, so the secret refusal still applies
    (SecretLeakError). Returns the path."""
    from . import writes
    item = _read(slug, item_id)
    if item is None:
        raise FileNotFoundError(item_id)
    if sha256 and sha256 != item.get("sha256"):
        raise HeldChanged(item_id)
    cur = _canonical(slug, item["path"])
    if (_sha(cur) if cur is not None else None) != item.get("base_sha256") and not force:
        raise HeldStale(item_id)
    content = base64.b64decode(item["content_b64"])
    await writes.apply_write(slug, item["path"], content)
    taintpaths.record(slug, [item["path"]], False)
    _drop(slug, item_id)
    await _audit("always_loaded_approved", "info", slug,
                 f"held change to always-loaded file {item['path']} approved",
                 {"path": item["path"], "by": "operator"})
    return item["path"]


async def reject(slug: str, item_id: str) -> str | None:
    item = _read(slug, item_id)
    if item is None or not _drop(slug, item_id):
        return None
    await _audit("always_loaded_rejected", "info", slug,
                 f"held change to always-loaded file {item['path']} rejected",
                 {"path": item["path"], "by": "operator"})
    return item["path"]


def prompt_note(slug: str) -> str:
    """One short block for the prompt, only when something is held: the agent
    wrote to a file that rides every prompt and the write is waiting, so it does
    not read the old text, conclude its edit was lost and write it again. Fixed
    words plus paths the OPERATOR listed; nothing an agent wrote."""
    held = held_paths(slug)
    if not held:
        return ""
    return ("# Edits waiting for the operator\n"
            "Your earlier changes to these always-loaded files are held for the operator's "
            "approval (the turn that made them had read untrusted content). Until they "
            "approve, the file is as it was; do not write it again.\n"
            + "\n".join(f"- {p}" for p in held))


# --- alerts --------------------------------------------------------------------

async def _audit(kind: str, severity: str, slug: str, summary: str,
                 detail: dict | None = None, cause: str | None = None) -> None:
    """Best-effort: the write or the approval stands whether or not the alert can
    be recorded."""
    try:
        from . import security
        from .db import get_db
        db = await get_db()
        try:
            await security.raise_event(
                db, kind=kind, severity=severity, project=slug, summary=summary,
                detail={**(detail or {}), "conversation_id": runtime.conversation_id.get()},
                cause=cause)
        finally:
            await db.close()
    except Exception:  # noqa: BLE001
        pass


def _notify(slug: str, rel: str) -> None:
    """A toast on the shared notices stream (the Memory badge counts it). Not a
    security event on its own; the held-change event above is that."""
    try:
        from . import bus
        from .agents_run import NOTICE_CHAN
        from .memory import flat_line
        bus.publish(NOTICE_CHAN, {
            "type": "memory_pending",
            "title": "Jav3 wrote to a file that rides every prompt; held for you",
            "summary": flat_line(f"{slug}: {rel}", 80), "to": "/memory"})
    except Exception:  # noqa: BLE001
        pass
