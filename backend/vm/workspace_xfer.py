"""Workspace transfer for guest-run turns.

The guest edits a COPY of the project, never the canonical files directly. So:
- `build_merged_tar(slug)` ships the project's workspace into the guest, minus
  junk and any legacy `.staging` dir (the guest uses its own as a write buffer).
- `apply_guest_writes(slug, tar)` takes back the guest's write buffer and applies
  each file through the HOST `writes.apply_write` — so the PROTECTED guard, 0644,
  the secret-leak refusal and the advisory diff-gate scan stay authoritative
  host-side. With the staging quarantine removed this lands files on canonical
  immediately; git is the review/undo surface.
"""
import io
import logging
import tarfile
from pathlib import Path

from fastapi import HTTPException

from .. import taintpaths, writes
from ..config import settings
from ..fsutil import list_tree

log = logging.getLogger("jav3.workspace_xfer")

SKIP = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist",
        ".workspace.json", ".context.json", ".staging"}


# what a turn's writes may NOT bring back. `dist` is skipped going IN (build
# output is regenerated) but kept coming OUT: a build the agent made on purpose
# was dropped here while run_code told it "kept" (dist/voxelcraft.html,
# 2026-09-27). run_code's per-file and per-run caps still bound its size.
SKIP_OUT = SKIP - {"dist"}


def _skip(rel: str, skip=SKIP) -> bool:
    return any(part in skip for part in Path(rel).parts)


def build_merged_tar(slug: str) -> bytes:
    """The project's current files, minus SKIP."""
    proj = settings.projects_dir / slug
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in sorted(list_tree(proj), key=lambda e: e["path"]):
            rel = entry["path"]
            if _skip(rel):
                continue
            p = writes.resolve(slug, rel)
            if p is None or not p.is_file():
                continue
            data = p.read_bytes()
            ti = tarfile.TarInfo(rel)
            ti.size = len(data)
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


async def apply_guest_writes(slug: str, tar_bytes: bytes, op_id: str | None = None) -> dict:
    """Apply the guest's write buffer to the canonical files host-side. Returns
    the applied rel-paths, any refused secret leaks (rel -> [secret names]), any
    advisory flags raised (rel -> [triggers]), `held`: writes to files that
    ride every prompt (alwaysloaded.py) that a tainted turn made, which wait for
    the operator instead of landing, and what did NOT land for any other reason:
    `refused` (rel -> why: a protected or excluded path) and `failed` (rel ->
    the error). Nothing here is silent: describe_unapplied() turns the result
    into what the reader is told.

    Tainted = the turn that owns `op_id` read untrusted content, or any turn on
    the project did while live or since the project was last idle (the buffer is
    the project's, and turns share it, so the wider reading is the safe one)."""
    applied: list[str] = []
    leaks: dict[str, list[str]] = {}
    flagged: dict[str, list[str]] = {}
    held: list[str] = []
    refused: dict[str, str] = {}
    failed: dict[str, str] = {}
    res = {"applied": applied, "secret_files": leaks, "flags": flagged, "held": held,
           "refused": refused, "failed": failed}
    if not tar_bytes:
        return res
    from . import broker
    tainted = bool(broker.project_tainted(slug, consume=True)
                   or (op_id and broker.op_tainted(op_id)))
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            rel = m.name
            if _skip(rel, SKIP_OUT):
                refused[rel] = "an excluded path (.git, .staging, node_modules, ...)"
                continue
            f = tar.extractfile(m)
            if f is None:
                continue
            data = f.read()
            try:
                triggers, was_held = await writes.apply_write_gated(
                    slug, rel, data, tainted=tainted)
            except writes.SecretLeakError as e:
                leaks[rel] = e.names       # refused — never lands canonical
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
                held.append(rel)           # not on disk: not in the tainted-paths ledger either
                continue
            if triggers:
                flagged[rel] = triggers
            applied.append(rel)
    # remember which of these files a tainted turn wrote (backend/taintpaths.py):
    # a later turn that reads one is told, by the guest, that it read untrusted
    # text; a clean turn's write of a path clears it
    taintpaths.record(slug, applied, tainted)
    return res


# What the reader is told when the guest's changes did not all land. The agent
# was told "write_file ok" in the guest and its history says the files exist, so
# without this a refused or failed file is simply gone next turn.
LOST_NOTE = ("\n\n[This turn's file changes could not be brought back from the guest to "
             "the project, so files it reported as written may be missing. Check "
             "list_files before relying on them.]")

_NOTE_MAX = 8


def describe_unapplied(res: dict | None) -> str:
    """'' when everything landed, else bracketed notes naming each file that did
    not (and why), for the end of the turn's answer."""
    if not res:
        return ""
    bits: list[str] = []
    for rel, names in (res.get("secret_files") or {}).items():
        hint = ", ".join("{{secret:%s}}" % n for n in names)
        bits.append(f"{rel} (refused: it contains the value of the stored secret "
                    f"{', '.join(names)}; write {hint} instead)")
    for rel, why in (res.get("refused") or {}).items():
        bits.append(f"{rel} (refused: {why})")
    for rel, why in (res.get("failed") or {}).items():
        bits.append(f"{rel} (write failed: {why})")
    out = ""
    if bits:
        more = f"; and {len(bits) - _NOTE_MAX} more" if len(bits) > _NOTE_MAX else ""
        out += ("\n\n[Not saved to the project, although the guest reported them written: "
                + "; ".join(bits[:_NOTE_MAX]) + more + ".]")
    held = res.get("held") or []
    if held:
        out += ("\n\n[Held for the operator's approval (this turn had read untrusted "
                "content): " + ", ".join(held[:_NOTE_MAX]) + ". The project still has "
                "the previous text.]")
    return out
