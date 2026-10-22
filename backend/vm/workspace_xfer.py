"""Workspace transfer for guest-run turns.

The guest edits a COPY of the project, never the canonical files directly. So:
- `build_merged_tar(slug)` ships the project's workspace into the guest, minus
  junk and any legacy `.staging` dir (the guest uses its own as a write buffer).
  Dotfiles ship too (.gitignore, .eslintrc, .github/): without them the guest
  saw a project with no .gitignore and an agent wrote one that replaced the
  real one. Credential files stay out (see _withheld).
- `apply_guest_writes(slug, tar)` takes back the guest's write buffer and applies
  each file through the HOST `writes.apply_write` — so the PROTECTED guard, 0644,
  the secret-leak refusal and the advisory diff-gate scan stay authoritative
  host-side. With the staging quarantine removed this lands files on canonical
  immediately; git is the review/undo surface.
- The buffer also names the files the guest deleted or renamed away (the shell's
  rm and mv act on the copy, not the buffer). Each goes through
  `writes.apply_delete`, and only if the host still has the file exactly as it
  last shipped it (see _shipped): an untrusted guest cannot delete a file it was
  not shown, or one that changed under it.
"""
import asyncio
import hashlib
import io
import json
import logging
import tarfile
import zlib
from pathlib import Path

from fastapi import HTTPException

from .. import secrets as secrets_mod
from .. import taintpaths, writes
from ..config import settings
from ..fsutil import list_tree

log = logging.getLogger("jav3.workspace_xfer")

SKIP = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
        ".workspace.json", ".context.json", ".staging"}

# `dist` is build output the agent made on purpose: it comes OUT of the guest
# (a build was dropped here while run_code told it "kept", dist/voxelcraft.html,
# 2026-09-27) and, because it was skipped going IN, every later turn saw a
# project with no dist/ (benchmark-game, 2026-10-01). It goes in now, unless it
# is bigger than this: then it stays out and the turn is told (workspace_note).
DIST_MAX_BYTES = 16 * 1024 * 1024

# what a turn's writes may NOT bring back (run_code's per-file and per-run caps
# still bound the size of a build)
SKIP_OUT = set(SKIP)

# Dot dirs a tool run writes under HOME (run_code sets HOME to the project): a
# Chromium, pip or npm run leaves .config, .local, .pki and .cache there. They
# are dropped coming out, unless the project has such a dir of its own (a file
# of it tracked in git, or shipped into the guest): see _owned_home_dirs.
HOME_DOT_DIRS = {".config", ".local", ".pki", ".cache"}

# ...of which these are the harness's own: a write to one is REFUSED and said so.
# The rest (.venv, node_modules, __pycache__, .pytest_cache) is generated junk
# that run_code sweeps up by accident and is dropped quietly, as it always was.
PROTECTED_OUT = {".git", ".staging", ".workspace.json", ".context.json"}

# The guest's list of files it deleted or renamed away rides the buffer as this
# member. `.staging` is a name the guest's own write tools refuse, so no file the
# agent writes can collide with it; a hand-made one is checked like a real one.
DELETED_MEMBER = ".staging/deleted.json"
MAX_DELETIONS = 5000
MAX_LIST_BYTES = 4 * 1024 * 1024

# What one buffer from the guest may unpack to (the guest is the hostile side: a
# 0.9 MB gzip once became a 200 MB file and 730 MB of host memory). run_code
# keeps a file to 2 MB and a run to 8 MB, so these leave headroom for a long turn.
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_BUFFER_BYTES = 128 * 1024 * 1024
MAX_STREAM_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 20000

# What the host last shipped or applied, per project: {rel: sha256}. A deletion
# the guest reports is honoured only for a file listed here whose bytes on the
# host still match. Applied writes update it (host and guest then agree on that
# content), so a file flushed mid-turn and deleted later still goes.
_shipped: dict[str, dict[str, str]] = {}

# slug -> what the last workspace build left out that the turn should be told
# (a dist/ over DIST_MAX_BYTES); "" when it left nothing out
_notes: dict[str, str] = {}

# Dotfiles that ship into the guest are only those with no credentials in them.
_SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".docker"}
_SECRET_FILES = {".netrc", ".pypirc", ".git-credentials"}
_ENV_TEMPLATE = {"example", "sample", "template", "dist", "defaults"}


def _skip(rel: str, skip=SKIP) -> bool:
    return any(part in skip for part in Path(rel).parts)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _withheld(rel: str) -> bool:
    """A file that stays out of the guest although dotfiles go in: env files
    (real values live in them), credential files and dirs. A project's
    `.env.example` is a template and ships."""
    p = Path(rel)
    if any(part in _SECRET_DIRS for part in p.parts):
        return True
    name = p.name
    if name in _SECRET_FILES or name == ".env":
        return True
    return name.startswith(".env.") and name.rsplit(".", 1)[-1] not in _ENV_TEMPLATE


def _is_dot(rel: str) -> bool:
    return any(part.startswith(".") for part in Path(rel).parts)


def _in_dist(rel: str) -> bool:
    return "dist" in Path(rel).parts[:-1]


def _home_dir(rel: str) -> str | None:
    """The HOME-style dot dir a path sits under (".config", "app/.local"), or None."""
    parts = Path(rel).parts
    for i, part in enumerate(parts[:-1]):
        if part in HOME_DOT_DIRS:
            return "/".join(parts[:i + 1])
    return None


def build_merged_tar(slug: str) -> bytes:
    """The project's current files, minus SKIP. Records what it shipped (and each
    file's digest) so the guest's deletions can be checked when they come home."""
    return build_workspace(slug)[0]


def workspace_note(slug: str) -> str:
    """What the last workspace build for `slug` left out that the turn must be
    told, or "" (a dist/ over DIST_MAX_BYTES)."""
    return _notes.get(slug, "")


def build_workspace(slug: str) -> tuple[bytes, str]:
    """(the tarball build_merged_tar describes, a note for the turn or "")."""
    proj = settings.projects_dir / slug
    buf = io.BytesIO()
    shipped: dict[str, str] = {}
    entries = [e for e in sorted(list_tree(proj, dotfiles=True), key=lambda e: e["path"])
               if not (_skip(e["path"]) or _withheld(e["path"]))]
    note = ""
    dist_bytes = sum(e["size"] for e in entries if _in_dist(e["path"]))
    if dist_bytes > DIST_MAX_BYTES:
        entries = [e for e in entries if not _in_dist(e["path"])]
        note = (f"[Workspace note: the project's dist/ build output is {_mb(dist_bytes)}, "
                f"over the {_mb(DIST_MAX_BYTES)} the copy of the project in this box carries, "
                "so it was not copied in. It is still in the project; a build you make here "
                "comes back as usual.]")
        log.info("not shipping dist/ of %s into the guest: %s", slug, _mb(dist_bytes))
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in entries:
            rel = entry["path"]
            p = writes.resolve(slug, rel)
            if p is None or not p.is_file():
                continue
            data = p.read_bytes()
            if (_is_dot(rel) or _in_dist(rel)) and secrets_mod.find_in_bytes(data):
                # a dotfile (or built file) holding a stored secret's value must
                # not reach the guest (the guest holds no secrets)
                log.warning("not shipping %s/%s into the guest: it contains a stored secret",
                            slug, rel)
                continue
            ti = tarfile.TarInfo(rel)
            ti.size = len(data)
            ti.mode = 0o644
            # the real mtime: without it every file in the guest is 1970-01-01 and
            # make, pytest's cache, rsync -u and anything comparing times misjudge
            # (BUILD-14)
            ti.mtime = int(p.stat().st_mtime)
            tar.addfile(ti, io.BytesIO(data))
            shipped[rel] = _sha(data)
    _shipped[slug] = shipped
    _notes[slug] = note
    return buf.getvalue(), note


def note_shipped(slug: str, rel: str, data: bytes) -> None:
    """The host put `rel` into the live guest copy mid-turn (guest_turn.push_files):
    it counts as shipped, so the guest's later deletion of it is honoured."""
    _shipped.setdefault(slug, {})[rel] = _sha(data)


def _mb(n: int) -> str:
    return f"{n / (1 << 20):.3g} MB"


def _read_buffer(tar_bytes: bytes) -> tuple[list[tuple[str, bytes]], dict[str, str], str | None]:
    """The regular files of the guest's buffer as (name, bytes), read within
    bounds: a file over MAX_MEMBER_BYTES or past MAX_BUFFER_BYTES in all is left
    out with its reason, and reading stops at MAX_MEMBERS files or once the tar
    has declared MAX_STREAM_BYTES (a bomb is refused by its headers, before it is
    unpacked). Returns (files, left_out {name: why}, why reading stopped or None).
    A buffer that is not a tar at all raises; one that breaks partway returns
    what was read before it."""
    items: list[tuple[str, bytes]] = []
    left_out: dict[str, str] = {}
    declared = kept = 0
    stopped = None
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        try:
            for m in tar:
                if not m.isfile():
                    continue
                declared += m.size
                if len(items) + len(left_out) >= MAX_MEMBERS:
                    stopped = f"more than {MAX_MEMBERS:,} files"
                    break
                if declared > MAX_STREAM_BYTES:
                    stopped = f"more than {_mb(MAX_STREAM_BYTES)} in all"
                    break
                limit = MAX_LIST_BYTES if m.name == DELETED_MEMBER else MAX_MEMBER_BYTES
                if m.size > limit:
                    left_out[m.name] = f"larger than {_mb(limit)}"
                    continue
                if kept + m.size > MAX_BUFFER_BYTES:
                    left_out[m.name] = f"the buffer is over {_mb(MAX_BUFFER_BYTES)}"
                    continue
                f = tar.extractfile(m)
                if f is None:
                    continue
                data = f.read(m.size)
                kept += len(data)
                items.append((m.name, data))
        except (tarfile.TarError, EOFError, OSError, zlib.error) as e:
            stopped = f"the buffer is damaged ({type(e).__name__})"
    return items, left_out, stopped


def _keep_harness_ignores(data: bytes) -> bytes:
    """The project's root .gitignore always keeps the harness's own lines
    (.staging/, .workspace.json, .context.json, data/). An agent that rewrites the
    file drops them, and the next `git add -A` would stage the buffer, the
    workspace file, the context list and the data dir. Missing lines are put back
    at the end; whatever else the agent wrote stays."""
    from .. import gitgate
    have = {line.strip() for line in data.splitlines()}
    missing = [ln for ln in gitgate.GITIGNORE.splitlines()
               if ln.strip() and ln.strip().encode() not in have]
    if not missing:
        return data
    sep = b"" if not data or data.endswith(b"\n") else b"\n"
    return data + sep + ("\n".join(missing) + "\n").encode()


async def apply_guest_writes(slug: str, tar_bytes: bytes, op_id: str | None = None) -> dict:
    """Apply the guest's write buffer to the canonical files host-side. Returns
    the applied rel-paths, any refused secret leaks (rel -> [secret names]), any
    advisory flags raised (rel -> [triggers]), `held`: writes to files that
    ride every prompt (alwaysloaded.py) that a tainted turn made, which wait for
    the operator instead of landing, and what did NOT land for any other reason:
    `refused` (rel -> why: a protected path) and `failed` (rel -> the error).
    Files the guest deleted or renamed away come back as `deleted`, and
    `not_deleted` (rel -> why the file was kept). Nothing here is silent:
    describe_unapplied() turns the result into what the reader is told.

    Tainted = the turn that owns `op_id` read untrusted content, or any turn on
    the project did while live or since the project was last idle (the buffer is
    the project's, and turns share it, so the wider reading is the safe one)."""
    applied: list[str] = []
    leaks: dict[str, list[str]] = {}
    flagged: dict[str, list[str]] = {}
    held: list[str] = []
    refused: dict[str, str] = {}
    failed: dict[str, str] = {}
    deleted: list[str] = []
    not_deleted: dict[str, str] = {}
    res = {"applied": applied, "secret_files": leaks, "flags": flagged, "held": held,
           "refused": refused, "failed": failed, "deleted": deleted,
           "not_deleted": not_deleted}
    if not tar_bytes:
        return res
    from . import broker
    tainted = bool(broker.project_tainted(slug, consume=True)
                   or (op_id and broker.op_tainted(op_id)))
    known = _shipped.setdefault(slug, {})
    applied_sha: dict[str, str] = {}
    wanted_deleted: list = []
    # the buffer is the guest's: read it with bounds, off the loop (a gzip bomb
    # used to unpack to hundreds of MB in one go)
    items, too_big, stopped = await asyncio.to_thread(_read_buffer, tar_bytes)
    refused.update(too_big)
    if stopped:
        refused["(the rest of the buffer)"] = stopped
    if too_big or stopped:
        log.warning("guest buffer for %s over the limits: %s",
                    slug, stopped or f"{len(too_big)} file(s)")
        await writes._raise_flag(slug, next(iter(too_big), "(guest write buffer)"), "oversize",
                                 {"files": list(too_big)[:20], "stopped": stopped})
    homes = {h for rel, _ in items if (h := _home_dir(rel))}
    owned = await _owned_home_dirs(slug, homes, known) if homes else set()
    dropped_homes = 0
    for rel, data in items:
        if rel == DELETED_MEMBER:
            try:
                listed = json.loads(data)
            except ValueError:
                listed = None
            if isinstance(listed, list):
                wanted_deleted = listed[:MAX_DELETIONS]
            else:
                log.warning("the guest's deletion list for %s is not a JSON list", slug)
            continue
        if set(Path(rel).parts) & PROTECTED_OUT:
            refused[rel] = "a protected path"
            continue
        if _skip(rel, SKIP_OUT):
            continue                       # generated junk, dropped quietly as ever
        home = _home_dir(rel)
        if home and home not in owned:
            dropped_homes += 1             # a tool run's HOME droppings, dropped the same way
            continue
        if rel == ".gitignore":
            data = _keep_harness_ignores(data)
        try:
            triggers, was_held = await writes.apply_write_gated(
                slug, rel, data, tainted=tainted)
        except writes.SecretLeakError as e:
            leaks[rel] = e.names           # refused — never lands canonical
            continue
        except (ValueError, HTTPException) as e:
            # a protected path, a path that escapes the project, or a change
            # too large to hold for approval: the write's own refusal
            refused[rel] = str(getattr(e, "detail", None) or e) or type(e).__name__
            continue
        except Exception as e:  # noqa: BLE001 — one bad path must not drop the rest
            failed[rel] = f"{type(e).__name__}: {e}"[:200]
            log.warning("guest write to %s/%s failed: %s", slug, rel, failed[rel])
            await writes._raise_flag(slug, rel, "write_failed", {"error": failed[rel]})
            continue
        if was_held:
            held.append(rel)               # not on disk: not in the tainted-paths ledger either
            continue
        if triggers:
            flagged[rel] = triggers
        applied.append(rel)
        applied_sha[rel] = known[rel] = _sha(data)
    if dropped_homes:
        log.info("dropped %d file(s) under HOME-style dot dirs (%s) from %s's buffer: not "
                 "the project's own", dropped_homes, ", ".join(sorted(homes - owned)), slug)
    old_sha: dict[str, str] = {}
    if wanted_deleted:
        if leaks or failed or refused:
            # a rename whose new half was refused must not lose its old half: the
            # deletion waits (the guest sends it again; at turn end the note says so)
            for rel in dict.fromkeys(r for r in wanted_deleted if isinstance(r, str)):
                if not _skip(rel):
                    not_deleted[rel] = "a file in the same batch was refused or failed"
        else:
            old_sha = await _apply_deletions(slug, wanted_deleted, tainted, known,
                                             deleted, not_deleted)
    # remember which of these files a tainted turn wrote (backend/taintpaths.py):
    # a later turn that reads one is told, by the guest, that it read untrusted
    # text; a clean turn's write of a path clears it. A deleted file leaves the
    # ledger, but a tainted file that was RENAMED (its bytes now under a new name
    # in this batch) keeps its taint under the new name.
    ledger = set(taintpaths.paths(slug)) if deleted else set()
    moved = {old_sha[r] for r in deleted if r in ledger and r in old_sha}
    taintpaths.record(slug, applied, tainted)
    if deleted:
        taintpaths.record(slug, deleted, False)
        taintpaths.record(slug, [r for r, h in applied_sha.items() if h in moved], True)
    return res


async def _owned_home_dirs(slug: str, homes: set[str], known: dict[str, str]) -> set[str]:
    """Which of these HOME-style dot dirs are the project's own: one a file of
    which went into the guest this turn, or is tracked in the project's git.
    Writes under any other are a tool run's droppings (a browser profile, pip's
    user dir) and stay out of the project."""
    from .. import gitgate
    owned: set[str] = set()
    has_git = (settings.projects_dir / slug / ".git").exists()
    for home in sorted(homes)[:50]:
        if any(k.startswith(home + "/") for k in known):
            owned.add(home)
            continue
        if not has_git:
            continue
        try:
            rc, out, _ = await gitgate.run_git(slug, "ls-files", "-z", "--", home)
        except Exception as e:  # noqa: BLE001 — cannot tell: keep the dir, as before
            log.warning("could not ask git about %s/%s: %s", slug, home, e)
            owned.add(home)
            continue
        if rc == 0 and out.strip("\0"):
            owned.add(home)
    return owned


async def _apply_deletions(slug: str, wanted: list, tainted: bool, known: dict,
                           deleted: list, not_deleted: dict) -> dict[str, str]:
    """Delete what the guest says it deleted, where the host agrees. Returns
    {rel: sha of the file as it was} for the deleted ones."""
    old_sha: dict[str, str] = {}
    for rel in dict.fromkeys(r for r in wanted if isinstance(r, str) and r):
        if _skip(rel):
            continue                       # never shipped, nothing to delete
        if rel == ".gitignore":
            not_deleted[rel] = "the project's .gitignore is kept"
            continue
        try:
            p = writes.resolve(slug, rel)
        except HTTPException:
            not_deleted[rel] = "a path outside the project"
            continue
        if p is None:
            known.pop(rel, None)           # already gone: a repeat of an earlier report
            continue
        want = known.get(rel)
        if want is None:
            not_deleted[rel] = "not a file this turn started with"
            continue
        try:
            if _sha(p.read_bytes()) != want:
                not_deleted[rel] = "changed in the project since this turn started"
                continue
            await writes.apply_delete(slug, rel, tainted=tainted)
        except FileNotFoundError:
            known.pop(rel, None)
            continue
        except (ValueError, HTTPException) as e:
            not_deleted[rel] = str(getattr(e, "detail", None) or e) or type(e).__name__
            continue
        except Exception as e:  # noqa: BLE001 — one bad path must not drop the rest
            not_deleted[rel] = f"{type(e).__name__}: {e}"[:200]
            log.warning("guest delete of %s/%s failed: %s", slug, rel, not_deleted[rel])
            await writes._raise_flag(slug, rel, "write_failed", {"error": not_deleted[rel]})
            continue
        deleted.append(rel)
        old_sha[rel] = known.pop(rel)
        log.info("guest turn deleted %s/%s", slug, rel)
    return old_sha


# What the reader is told when the guest's changes did not all land. The agent
# was told "write_file ok" in the guest and its history says the files exist, so
# without this a refused or failed file is simply gone next turn.
LOST_NOTE = ("\n\n[This turn's file changes could not be brought back from the guest to "
             "the project, so files it reported as written may be missing. Check "
             "list_files before relying on them.]")

_NOTE_MAX = 8


def _short(rel: str) -> str:
    """A path as the guest named it, bounded: it lands in a message the model reads."""
    rel = str(rel)
    return rel if len(rel) <= 120 else rel[:117] + "..."


def describe_unapplied(res: dict | None) -> str:
    """'' when everything landed, else bracketed notes naming each file that did
    not (and why), for the end of the turn's answer."""
    if not res:
        return ""
    bits: list[str] = []
    for rel, names in (res.get("secret_files") or {}).items():
        hint = ", ".join("{{secret:%s}}" % n for n in names)
        bits.append(f"{_short(rel)} (refused: it contains the value of the stored secret "
                    f"{', '.join(names)}; write {hint} instead)")
    for rel, why in (res.get("refused") or {}).items():
        bits.append(f"{_short(rel)} (refused: {why})")
    for rel, why in (res.get("failed") or {}).items():
        bits.append(f"{_short(rel)} (write failed: {why})")
    out = ""
    if bits:
        more = f"; and {len(bits) - _NOTE_MAX} more" if len(bits) > _NOTE_MAX else ""
        out += ("\n\n[Not saved to the project, although the guest reported them written: "
                + "; ".join(bits[:_NOTE_MAX]) + more + ".]")
    kept = [f"{_short(rel)} ({why})" for rel, why in (res.get("not_deleted") or {}).items()]
    if kept:
        more = f"; and {len(kept) - _NOTE_MAX} more" if len(kept) > _NOTE_MAX else ""
        out += ("\n\n[Not deleted, still in the project although the guest removed them: "
                + "; ".join(kept[:_NOTE_MAX]) + more + ".]")
    held = res.get("held") or []
    if held:
        out += ("\n\n[Held for the operator's approval (this turn had read untrusted "
                "content): " + ", ".join(_short(r) for r in held[:_NOTE_MAX])
                + ". The project still has "
                "the previous text.]")
    return out
