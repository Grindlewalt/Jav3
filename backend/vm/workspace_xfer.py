"""Workspace transfer for guest-run turns.

The guest edits a COPY of the project, never the operator's canonical files. So:
- `build_merged_tar(slug)` ships the project's effective workspace into the guest —
  canonical files with Jarvis's own pending staged edits overlaid, minus junk and
  the `.staging` dir itself (the guest re-stages its own).
- `reconcile_staged(slug, tar)` takes back the guest's `.staging` and re-applies
  each file through the HOST `staging.stage_write`, so the operator's PROTECTED
  guard, 0644, and artifact auto-approve stay authoritative, and every returned
  file is scanned for secret leaks (`secrets.find_in_bytes`). Approval
  (staging -> canonical) remains entirely host-side.

This mirrors the proven push/pull + _stage_changes shape from the old sandbox.
"""
import io
import tarfile
from pathlib import Path

from .. import secrets, staging
from ..config import settings
from ..fsutil import list_tree

SKIP = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist",
        ".workspace.json", ".context.json", ".staging"}


def _skip(rel: str) -> bool:
    return any(part in SKIP for part in Path(rel).parts)


def build_merged_tar(slug: str) -> bytes:
    """The project's effective view (canonical + staged overlay), minus SKIP."""
    proj = settings.projects_dir / slug
    rels = {e["path"] for e in list_tree(proj)}
    rels |= {e["path"] for e in staging.list_staged(slug)}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in sorted(rels):
            if _skip(rel):
                continue
            p = staging.effective_read(slug, rel)   # staged wins over canonical
            if p is None or not p.is_file():
                continue
            data = p.read_bytes()
            ti = tarfile.TarInfo(rel)
            ti.size = len(data)
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def reconcile_staged(slug: str, tar_bytes: bytes) -> dict:
    """Re-stage the guest's `.staging` files host-side. Returns the staged
    rel-paths and any secret leaks found (rel -> [secret names])."""
    staged: list[str] = []
    leaks: dict[str, list[str]] = {}
    if not tar_bytes:
        return {"staged": staged, "secret_files": leaks}
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
            hits = secrets.find_in_bytes(data)
            if hits:
                leaks[rel] = hits
            try:
                staging.stage_write(slug, rel, data)   # host PROTECTED/0644/auto-approve
                staged.append(rel)
            except Exception:  # noqa: BLE001 — one bad path must not drop the rest
                continue
    return {"staged": staged, "secret_files": leaks}
