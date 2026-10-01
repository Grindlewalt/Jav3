"""Direct file writes: an agent's file mutations land on the canonical project
files the moment they happen. The staging quarantine is gone (operator decision,
2026-07-19) — the VM is the execution boundary and git is the review/undo
surface. What survives from the old gate, enforced here at the one write
chokepoint:

  - path safety      safe_join + PROTECTED (never .git, never legacy .staging)
  - no exec bits     everything lands 0644
  - secret leaks     a write containing a real secret VALUE is REFUSED (the
                     {{secret:X}} indirection exists so keys never sit in
                     agent-reachable files) and raises a security event
  - diff-gate scan   diffgate.scan runs on every write as an ADVISORY tripwire:
                     the write lands, a deduped security event alerts the
                     operator (Review Center + bell). It no longer blocks.
                     A flag diffgate.judge calls normal work (a scratch file
                     the run made and threw away, an import the project
                     already makes, tool output) is still recorded, filed
                     quietly with the reason, and does not alert.
  - always-loaded    apply_write_gated: a write by a TAINTED turn to a file that
                     rides every prompt (project.md, the operator's ticked
                     context files) is held for the operator instead of landing
                     (alwaysloaded.py). Only callers that know the turn's taint
                     use it; apply_write itself never holds anything.

The guest has its own backend/writes.py shim with this interface that buffers
into the workspace .staging/ tarball for turn-end reconcile — which also funnels
through apply_write here, so guest-authored files get the same scan + refusal.
"""
import asyncio
import os
import time
from pathlib import Path

from . import diffgate, runtime
from . import secrets as secrets_mod
from .config import settings
from .fsutil import safe_join

# .staging: legacy quarantine dirs may linger on disk; keep them inert.
# .context.json is the operator's list of always-loaded files (alwaysloaded.py):
# no write of an agent's, from any channel, may edit it (case-folded: a
# case-insensitive filesystem would land '.Context.json' on it).
PROTECTED = {".staging", ".git", ".context.json"}


# --- what diffgate.judge needs to know -----------------------------------------
# Files this run created: the removal of a file the run itself made is its own
# scratch work. Kept per (project, conversation); a restart forgets, and the log
# (a new_file flag from the same conversation) remembers what it can.
_CREATED_RUNS = 200
_CREATED_PER_RUN = 5000
_created: dict[tuple[str, int], set[str]] = {}


def _pkey(slug: str) -> str:
    """What the caches below are keyed by: the project's directory, so two
    state dirs (a test's, a restored backup) never share one."""
    return str(settings.projects_dir / slug)


def _note_created(slug: str, rel: str) -> None:
    cid = runtime.conversation_id.get()
    if cid is None:
        return
    files = _created.get((_pkey(slug), cid))
    if files is None:
        if len(_created) >= _CREATED_RUNS:
            _created.pop(next(iter(_created)))
        files = _created[(_pkey(slug), cid)] = set()
    if len(files) < _CREATED_PER_RUN:
        files.add(rel)


async def _created_here(slug: str, rel: str) -> bool:
    """Did this conversation create <rel>? (a file it wrote that was not there)"""
    cid = runtime.conversation_id.get()
    if cid is None:
        return False
    if rel in _created.get((_pkey(slug), cid), ()):
        return True
    try:
        from .db import get_db
        db = await get_db()
        try:
            async with db.execute(
                    "SELECT 1 FROM security_events WHERE kind = 'write_flag' "
                    "AND project_slug = ? AND json_extract(detail, '$.path') = ? "
                    "AND json_extract(detail, '$.conversation_id') = ? "
                    "AND json_extract(detail, '$.new_file') = 1 LIMIT 1",
                    (slug, rel, cid)) as cur:
                return await cur.fetchone() is not None
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — the log is a second opinion
        return False


async def _in_head(slug: str, rel: str) -> bool | None:
    """Does the project's git HEAD hold <rel>? A project with no repo or no
    commit holds nothing. None when git could not answer: that never excuses
    a flag."""
    if not (settings.projects_dir / slug / ".git").exists():
        return False
    try:
        from . import gitgate
        rc, _, _ = await gitgate.run_git(slug, "rev-parse", "--verify", "-q", "HEAD",
                                         timeout=10)
        if rc != 0:
            return False                    # nothing committed yet
        rc, _, _ = await gitgate.run_git(slug, "cat-file", "-e", f"HEAD:{rel}", timeout=10)
        return rc == 0
    except Exception:  # noqa: BLE001
        return None


# the outside modules a project already imports, from its own files (rescanned at
# most every _MODS_TTL seconds) plus every one a write has shown since this
# process started: "already imported elsewhere in the project" is not new
_MODS_TTL = 30.0
_MODS_FILES = 4000
_MODS_FILE_BYTES = 512 * 1024
_SRC_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_SKIP_DIRS = diffgate.TOOL_OUTPUT_DIRS | {".git", ".venv", "venv", "__pycache__", ".staging",
                                          ".config", "data"}
_mods: dict[str, tuple[float, set[str]]] = {}
_seen_mods: dict[str, set[str]] = {}


def _scan_project_modules(slug: str) -> set[str]:
    out: set[str] = set()
    n = 0
    for dirpath, dirnames, filenames in os.walk(settings.projects_dir / slug):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if diffgate.ext_of(fn) not in _SRC_EXT:
                continue
            n += 1
            if n > _MODS_FILES:
                return out
            f = Path(dirpath) / fn
            try:
                if f.stat().st_size <= _MODS_FILE_BYTES:
                    out |= diffgate.modules_of(f.read_text(errors="replace"), fn)
            except OSError:
                continue
    return out


async def _known_modules(slug: str) -> set[str]:
    key = _pkey(slug)
    hit = _mods.get(key)
    if hit is None or time.monotonic() - hit[0] > _MODS_TTL:
        hit = (time.monotonic(), await asyncio.to_thread(_scan_project_modules, slug))
        _mods[key] = hit
    return hit[1] | _seen_mods.get(key, set())


def _note_modules(slug: str, rel: str, text: str) -> None:
    mods = diffgate.modules_of(text, rel)
    if mods:
        _seen_mods.setdefault(_pkey(slug), set()).update(mods)
        if len(_seen_mods) > 500:
            _seen_mods.pop(next(iter(_seen_mods)))


async def _judge_all(slug: str, rel: str, flags: list[dict]) -> list[tuple[str | None, dict]]:
    """[(reason it is normal work or None, flag to raise)] for each flag. The
    facts are fetched only for the flags that need them."""
    in_head: bool | None = None
    created = False
    known: set[str] = set()
    if any(f["trigger"] in diffgate.REMOVALS for f in flags) and not diffgate.tool_output(rel):
        created = await _created_here(slug, rel)
        if not created:
            in_head = await _in_head(slug, rel)
    if any(f["trigger"] == "new_import" for f in flags) and not diffgate.tool_output(rel):
        known = await _known_modules(slug)
    return [diffgate.judge(f, rel, in_head=in_head, created_here=created, known_modules=known)
            for f in flags]


class SecretLeakError(ValueError):
    """The content contains the literal value of an operator secret."""

    def __init__(self, names: list[str]):
        self.names = names
        super().__init__(f"content contains secret value(s): {', '.join(names)}")


def resolve(slug: str, rel: str) -> Path | None:
    """The canonical file, or None if it doesn't exist. (The guest shim's
    version overlays the turn's pending writes; host-side a write IS the file.)"""
    p = safe_join(settings.projects_dir / slug, rel)
    return p if p.is_file() else None


def pending_paths(slug: str) -> dict[str, str]:
    """Host: nothing is ever pending — writes apply immediately. The guest shim
    returns its unreconciled overlay so in-guest listings show fresh files."""
    return {}


async def _check(slug: str, rel: str, content: bytes) -> Path:
    """The refusals every write gets, landing or held: the path (PROTECTED, no
    escape) and a real secret value. Returns the destination."""
    project = settings.projects_dir / slug
    dest = safe_join(project, rel)
    top = dest.relative_to(project.resolve()).parts[0]  # normalized: '../' resolved
    if top in PROTECTED or top.casefold() == ".context.json":
        raise ValueError(f"cannot write into {top}")

    leaks = secrets_mod.find_in_bytes(content)
    if leaks:
        # 'critical' is the schema's top severity (db.py: info|warn|critical);
        # the old 'alert' wasn't in the Review Center's map and styled as info
        await _raise_flag(slug, rel, "secret_leak",
                          {"secrets": leaks, "bytes": len(content)},
                          severity="critical", refused=True)
        raise SecretLeakError(leaks)
    return dest


async def apply_write_gated(slug: str, rel: str, content: bytes, *,
                            tainted: bool) -> tuple[list[str], bool]:
    """apply_write for a caller that knows whether the turn behind the write had
    read untrusted content. A tainted write to a file that rides every prompt of
    the project (alwaysloaded.is_loaded: project.md, the operator's ticked
    context files) is HELD for the operator instead of landing: same path and
    secret refusals, nothing written, the prompt keeps the file as it was.
    Everything else lands exactly as apply_write does. Returns (triggers, held)."""
    if tainted:
        from . import alwaysloaded
        if alwaysloaded.is_loaded(slug, rel):
            await _check(slug, rel, content)
            await alwaysloaded.hold(slug, rel, content)
            return [], True
    return await apply_write(slug, rel, content), False


async def apply_write(slug: str, rel: str, content: bytes) -> list[str]:
    """Write canonical content for <rel>. Returns the advisory flag triggers
    raised (empty for a clean write). Raises SecretLeakError (write refused)
    or ValueError (protected path)."""
    dest = await _check(slug, rel, content)

    old_text = ""
    existed = dest.is_file()
    if existed:
        try:
            old_text = dest.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            old_text = ""
    new_text = content.decode("utf-8", errors="replace")
    flags = diffgate.scan(old_text, new_text, rel)
    # judged before the write: what the project already imports, and whether
    # this run made the file, are as they were when the agent decided
    verdicts = await _judge_all(slug, rel, flags) if flags else []

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    dest.chmod(0o644)  # agent-written bytes never carry exec bits
    if not existed:
        _note_created(slug, rel)
    _note_modules(slug, rel, new_text)

    for reason, f in verdicts:
        await _raise_flag(slug, rel, f["trigger"],
                          {**f["detail"], "bytes": len(content),
                           "line_count": new_text.count("\n") + 1,
                           "new_file": not old_text}, rule=reason)
    return [f["trigger"] for f in flags]


class DeleteRefused(ValueError):
    """A delete the chokepoint will not make (see apply_delete)."""


async def apply_delete(slug: str, rel: str, *, tainted: bool = False) -> list[str]:
    """Delete canonical <rel>: the guest removed or renamed it. The path refusals
    of a write apply (PROTECTED, no escape). A turn that had read untrusted content
    may not delete a file that rides every prompt (alwaysloaded.py): a write to
    one is HELD for the operator, and a hold cannot express a deletion, so the
    delete is refused. The diff gate scans the removal as a rewrite to nothing
    (advisory: a deleted test file loses its assertions). Returns the flag
    triggers; raises FileNotFoundError when there is no such file, DeleteRefused
    or ValueError (protected path) when it will not."""
    dest = await _check(slug, rel, b"")
    if tainted:
        from . import alwaysloaded
        if alwaysloaded.is_loaded(slug, rel):
            raise DeleteRefused("a file that rides every prompt cannot be deleted by a "
                                "turn that read untrusted content")
    if not dest.is_file():
        raise FileNotFoundError(rel)
    try:
        old_bytes = dest.read_bytes()
    except OSError:
        old_bytes = b""
    flags = diffgate.scan(old_bytes.decode("utf-8", errors="replace"), "", rel)
    verdicts = await _judge_all(slug, rel, flags) if flags else []
    dest.unlink()
    # a directory the delete emptied goes too (git does not track it; an empty
    # folder left behind is only clutter), never past the project root
    project = (settings.projects_dir / slug).resolve()
    d = dest.parent
    while d != project and d.is_relative_to(project):
        try:
            d.rmdir()
        except OSError:
            break
        d = d.parent
    for reason, f in verdicts:
        await _raise_flag(slug, rel, f["trigger"],
                          {**f["detail"], "bytes": len(old_bytes), "deleted": True},
                          rule=reason)
    return [f["trigger"] for f in flags]


async def _raise_flag(slug: str, rel: str, trigger: str, detail: dict, *,
                      severity: str = "warn", refused: bool = False,
                      rule: str | None = None) -> None:
    """One deduped security event per (project, path, trigger): an agent
    iterating on a flagged file must not drown the bell. Best-effort — an
    alerting failure never fails the write it annotates.

    The detail carries the conversation that made the write, so the Review
    Center's board can name the run that did this instead of leaving the
    operator to guess which of several concurrent agents it was.

    `rule` is why diffgate.judge calls the flag normal work: it is recorded all
    the same (security.raise_event files it acknowledged, quiet='rule'), only it
    does not alert."""
    try:
        from . import security
        from .db import get_db
        summary = (f"write refused (secret leak) in {rel}" if refused
                   else f"write flag: {trigger} in {rel}")
        db = await get_db()
        try:
            if not rule:
                async with db.execute(
                    "SELECT 1 FROM security_events WHERE kind='write_flag' AND "
                    "project_slug = ? AND summary = ? AND acknowledged = 0",
                    (slug, summary)) as cur:
                    if await cur.fetchone():
                        return
            await security.raise_event(
                db, kind="write_flag", severity=severity, project=slug,
                summary=summary, rule=rule,
                detail={"path": rel, "trigger": trigger, "refused": refused,
                        "conversation_id": runtime.conversation_id.get(), **detail})
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — advisory only, never breaks the write
        pass
