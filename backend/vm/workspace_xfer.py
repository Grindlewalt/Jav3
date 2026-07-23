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
import tarfile
from pathlib import Path

from .. import writes
from ..config import settings
from ..fsutil import list_tree

SKIP = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist",
        ".workspace.json", ".context.json", ".staging"}


def _skip(rel: str) -> bool:
    return any(part in SKIP for part in Path(rel).parts)


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


async def apply_guest_writes(slug: str, tar_bytes: bytes) -> dict:
    """Apply the guest's write buffer to the canonical files host-side. Returns
    the applied rel-paths, any refused secret leaks (rel -> [secret names]) and
    any advisory flags raised (rel -> [triggers])."""
    applied: list[str] = []
    leaks: dict[str, list[str]] = {}
    flagged: dict[str, list[str]] = {}
    if not tar_bytes:
        return {"applied": applied, "secret_files": leaks, "flags": flagged}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            rel = m.name
            if _skip(rel):
                continue
            f = tar.extractfile(m)
            if f is None:
                continue
            data = f.read()
            try:
                triggers = await writes.apply_write(slug, rel, data)
            except writes.SecretLeakError as e:
                leaks[rel] = e.names       # refused — never lands canonical
                continue
            except Exception:  # noqa: BLE001 — one bad path must not drop the rest
                continue
            if triggers:
                flagged[rel] = triggers
            applied.append(rel)
    return {"applied": applied, "secret_files": leaks, "flags": flagged}
