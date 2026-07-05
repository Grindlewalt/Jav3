"""Staged writes: Jarvis's file mutations land here, not on the real files.

Every mutation a tool makes goes to projects/<slug>/.staging/<relpath> the
moment it happens — so a VM nuke never loses work — but the canonical files
stay untouched until the operator approves in the dashboard. Staged content
is inert by contract: never executed, never rendered live, never imported;
the GUI only ever shows it as text/diffs.
"""
import shutil
from pathlib import Path

from .config import settings
from .fsutil import safe_join

STAGING = ".staging"

# project.md is journal territory (journal_update), and staging metadata is ours.
PROTECTED = {STAGING}


def _staging_dir(slug: str) -> Path:
    return settings.projects_dir / slug / STAGING


def stage_write(slug: str, rel: str, content: bytes) -> Path:
    """Stage new content for <rel>. The canonical file is untouched."""
    if rel.split("/")[0] in PROTECTED:
        raise ValueError(f"cannot write into {rel.split('/')[0]}")
    base = _staging_dir(slug)
    base.mkdir(parents=True, exist_ok=True)
    dest = safe_join(base, rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    dest.chmod(0o644)  # staged bytes never carry exec bits
    return dest


def effective_read(slug: str, rel: str) -> Path | None:
    """The file as Jarvis should see it: its own staged edit if one exists,
    else the canonical file. Returns None if neither exists."""
    project = settings.projects_dir / slug
    staged = safe_join(_staging_dir(slug), rel) if _staging_dir(slug).exists() else None
    if staged and staged.is_file():
        return staged
    canonical = safe_join(project, rel)
    return canonical if canonical.is_file() else None


def list_staged(slug: str) -> list[dict]:
    base = _staging_dir(slug)
    project = settings.projects_dir / slug
    out = []
    if not base.exists():
        return out
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(base))
        canonical = project / rel
        status = "modified" if canonical.exists() else "new"
        if status == "modified" and canonical.read_bytes() == p.read_bytes():
            continue  # no-op edit; don't ask the operator to review nothing
        out.append({"path": rel, "status": status, "size": p.stat().st_size})
    return out


def approve(slug: str, paths: list[str] | None = None) -> list[str]:
    """Copy staged files over the canonical ones. None = approve everything."""
    base = _staging_dir(slug)
    project = settings.projects_dir / slug
    staged = {e["path"] for e in list_staged(slug)}
    chosen = staged if paths is None else (set(paths) & staged)
    applied = []
    for rel in sorted(chosen):
        src = safe_join(base, rel)
        dest = safe_join(project, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        src.unlink()
        applied.append(rel)
    _prune(base)
    return applied


def reject(slug: str, paths: list[str] | None = None) -> list[str]:
    base = _staging_dir(slug)
    staged = {e["path"] for e in list_staged(slug)}
    chosen = staged if paths is None else (set(paths) & staged)
    dropped = []
    for rel in sorted(chosen):
        safe_join(base, rel).unlink(missing_ok=True)
        dropped.append(rel)
    _prune(base)
    return dropped


def _prune(base: Path) -> None:
    """Drop empty dirs (and identical leftovers) so staging vanishes when clean."""
    if not base.exists():
        return
    for p in sorted(base.rglob("*"), reverse=True):
        if p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    if base.exists() and not any(base.iterdir()):
        base.rmdir()
