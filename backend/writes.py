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

The guest has its own backend/writes.py shim with this interface that buffers
into the workspace .staging/ tarball for turn-end reconcile — which also funnels
through apply_write here, so guest-authored files get the same scan + refusal.
"""
from pathlib import Path

from . import diffgate
from . import secrets as secrets_mod
from .config import settings
from .fsutil import safe_join

# .staging: legacy quarantine dirs may linger on disk; keep them inert.
PROTECTED = {".staging", ".git"}


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


async def apply_write(slug: str, rel: str, content: bytes) -> list[str]:
    """Write canonical content for <rel>. Returns the advisory flag triggers
    raised (empty for a clean write). Raises SecretLeakError (write refused)
    or ValueError (protected path)."""
    project = settings.projects_dir / slug
    dest = safe_join(project, rel)
    top = dest.relative_to(project.resolve()).parts[0]  # normalized: '../' resolved
    if top in PROTECTED:
        raise ValueError(f"cannot write into {top}")

    leaks = secrets_mod.find_in_bytes(content)
    if leaks:
        # 'critical' is the schema's top severity (db.py: info|warn|critical);
        # the old 'alert' wasn't in the Review Center's map and styled as info
        await _raise_flag(slug, rel, "secret_leak", {"secrets": leaks},
                          severity="critical", refused=True)
        raise SecretLeakError(leaks)

    old_text = ""
    if dest.is_file():
        try:
            old_text = dest.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            old_text = ""
    new_text = content.decode("utf-8", errors="replace")
    flags = diffgate.scan(old_text, new_text, rel)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    dest.chmod(0o644)  # agent-written bytes never carry exec bits

    for f in flags:
        await _raise_flag(slug, rel, f["trigger"], f["detail"])
    return [f["trigger"] for f in flags]


async def _raise_flag(slug: str, rel: str, trigger: str, detail: dict, *,
                      severity: str = "warn", refused: bool = False) -> None:
    """One deduped security event per (project, path, trigger): an agent
    iterating on a flagged file must not drown the bell. Best-effort — an
    alerting failure never fails the write it annotates."""
    try:
        from . import security
        from .db import get_db
        summary = (f"write refused (secret leak) in {rel}" if refused
                   else f"write flag: {trigger} in {rel}")
        db = await get_db()
        try:
            async with db.execute(
                "SELECT 1 FROM security_events WHERE kind='write_flag' AND "
                "project_slug = ? AND summary = ? AND acknowledged = 0",
                (slug, summary)) as cur:
                if await cur.fetchone():
                    return
            await security.raise_event(
                db, kind="write_flag", severity=severity, project=slug,
                summary=summary, detail={"path": rel, "trigger": trigger, **detail})
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — advisory only, never breaks the write
        pass
