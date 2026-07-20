"""Guest `writes` shim — same interface as the host module, different sink.

In the guest, a write buffers into the workspace copy's `.staging/` overlay
(never the unpacked canonical files) so the turn-end pack ships exactly the
files the turn touched. The host reconciles that tarball through ITS
`writes.apply_write`, where the secret scan + advisory diff gate live — the
guest is the untrusted side, so nothing security-relevant happens here.
`resolve` overlays the buffer so the agent reads its own pending edits.
"""
from pathlib import Path

from .config import settings
from .fsutil import safe_join

STAGING = ".staging"
PROTECTED = {STAGING, ".git"}


class SecretLeakError(ValueError):
    """Never raised in the guest (no secrets exist here) — defined so the
    shared tool handlers' imports resolve. The host raises the real one at
    reconcile."""

    def __init__(self, names):
        self.names = names
        super().__init__("secret leak")


def _overlay_dir(slug: str) -> Path:
    return settings.projects_dir / slug / STAGING


def resolve(slug: str, rel: str) -> Path | None:
    overlay = _overlay_dir(slug)
    if overlay.exists():
        p = safe_join(overlay, rel)
        if p.is_file():
            return p
    p = safe_join(settings.projects_dir / slug, rel)
    return p if p.is_file() else None


def pending_paths(slug: str) -> dict[str, str]:
    """rel -> 'new'|'modified' for this turn's unreconciled writes."""
    overlay = _overlay_dir(slug)
    out: dict[str, str] = {}
    if not overlay.exists():
        return out
    canonical = settings.projects_dir / slug
    for p in sorted(overlay.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(overlay))
            out[rel] = "modified" if (canonical / rel).is_file() else "new"
    return out


async def apply_write(slug: str, rel: str, content: bytes) -> list[str]:
    overlay = _overlay_dir(slug)
    overlay.mkdir(parents=True, exist_ok=True)
    dest = safe_join(overlay, rel)
    top = dest.relative_to(overlay.resolve()).parts[0]
    if top in PROTECTED:
        raise ValueError(f"cannot write into {top}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    dest.chmod(0o644)
    return []
