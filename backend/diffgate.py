"""Deterministic diff gates (Layer 6).

Grep/heuristic checks over a staged file's diff — NO model in the loop, because
an AI reviewing AI output is not a control. A trip is a SOFT flag: the write
still lands in staging (the agent keeps working), but a flagged file CANNOT be
approved until the operator acknowledges each flag.

Triggers the operator chose (large-unrelated-diff deliberately dropped as noise):
  new_import        — an added import / dependency (supply-chain injection)
  network_call      — an added outbound-call primitive (exfil / C2 / beacon)
  high_entropy      — an added base64/hex blob (embedded payload / obfuscation)
  secret_leak       — a known operator secret VALUE present in the staged bytes
  logging_removed   — fewer logging calls than the canonical file (evasion)
  assertion_removed — fewer assertions than the canonical file (test tampering)

`scan()` is pure and fully unit-testable; the DB layer persists flags and the
approve path re-checks them.
"""
import math
import re

import aiosqlite

from . import secrets as secrets_mod
from . import security, staging
from .config import settings
from .fsutil import safe_join

_PY_IMPORT = re.compile(r'^\s*(?:import\s+([\w.]+)|from\s+([\w.]+)\s+import)')
_JS_IMPORT = re.compile(r'''import\s.+from\s+["']([^"']+)["']|require\(\s*["']([^"']+)["']''')
_NET = re.compile(
    r'socket\.socket|socket\.create_connection|create_connection|requests\.'
    r'(?:get|post|put|patch|delete|request|head)|httpx\.|aiohttp|urllib\.request|'
    r'urlopen|\.connect\(|fetch\(|XMLHttpRequest|WebSocket|sendBeacon|EventSource|'
    r'/dev/tcp/|\bcurl\b|\bwget\b|nc\s+-', re.I)
_LOG = re.compile(
    r'logging\.|logger\.|\.getLogger|log\.(?:debug|info|warning|error|critical)|'
    r'console\.(?:log|error|warn|info)')
_ASSERT = re.compile(
    r'\bassert\s|\bassertEqual|\bassertTrue|\bassertFalse|\bassertRaises|'
    r'\bassertIn|\bexpect\(|\.should\b|\bassert!')
_B64 = re.compile(r'[A-Za-z0-9+/]{40,}={0,2}')
_HEX = re.compile(r'\b[0-9a-fA-F]{64,}\b')

# only files whose diff is worth gating (source/config); skip lockfiles & data
_CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".sh", ".bash",
             ".rb", ".go", ".rs", ".java", ".php", ".pl", ".c", ".h", ".cpp",
             ".yaml", ".yml", ".toml", ".cfg", ".ini", ".dockerfile"}


def _shannon(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    n = len(s)
    return -sum((k / n) * math.log2(k / n) for k in freq.values())


def _added_lines(old: str, new: str) -> list[str]:
    old_set = {ln.strip() for ln in old.splitlines()}
    return [ln for ln in new.splitlines() if ln.strip() and ln.strip() not in old_set]


def _imports(lines: list[str]) -> set[str]:
    mods: set[str] = set()
    for ln in lines:
        m = _PY_IMPORT.match(ln)
        if m:
            mods.add((m.group(1) or m.group(2)).split(".")[0])
        for jm in _JS_IMPORT.finditer(ln):
            mods.add(jm.group(1) or jm.group(2))
    return {m for m in mods if m}


def _count(rx: re.Pattern, text: str) -> int:
    return len(rx.findall(text))


def scan(old_text: str, new_text: str, path: str) -> list[dict]:
    """Deterministic flags for one file's canonical->staged diff. `old_text`
    is '' for a brand-new file. Returns [{trigger, detail}]."""
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext not in _CODE_EXT and not path.endswith("Dockerfile"):
        return []
    added = _added_lines(old_text, new_text)
    added_text = "\n".join(added)
    flags: list[dict] = []

    new_mods = _imports(added)
    if new_mods:
        flags.append({"trigger": "new_import", "detail": {"modules": sorted(new_mods)}})

    net = sorted({m.group(0) for m in _NET.finditer(added_text)})
    if net:
        flags.append({"trigger": "network_call", "detail": {"matches": net[:8]}})

    blobs = [b for b in _B64.findall(added_text) if _shannon(b) >= 4.0]
    blobs += _HEX.findall(added_text)
    if blobs:
        flags.append({"trigger": "high_entropy",
                      "detail": {"count": len(blobs), "sample": blobs[0][:24] + "…"}})

    if _count(_LOG, old_text) > _count(_LOG, new_text):
        flags.append({"trigger": "logging_removed",
                      "detail": {"before": _count(_LOG, old_text), "after": _count(_LOG, new_text)}})

    if _count(_ASSERT, old_text) > _count(_ASSERT, new_text):
        flags.append({"trigger": "assertion_removed",
                      "detail": {"before": _count(_ASSERT, old_text), "after": _count(_ASSERT, new_text)}})
    return flags


# --- persistence + enforcement ----------------------------------------------

def _read(p) -> tuple[str, bytes]:
    try:
        data = p.read_bytes()
        return data.decode("utf-8", errors="replace"), data
    except OSError:
        return "", b""


async def rescan_project(db: aiosqlite.Connection, slug: str) -> list[dict]:
    """Re-flag every staged file (canonical->staged diff + secret-value scan),
    reconciling gate_flags: new trips are inserted (and alerted once), trips that
    no longer hold or whose file left staging are removed. Acknowledged rows keep
    their state. Returns the current flag rows."""
    base = settings.projects_dir / slug / staging.STAGING
    project = settings.projects_dir / slug
    current: set[tuple[str, str]] = set()      # (path, trigger)
    details: dict[tuple[str, str], dict] = {}
    for entry in staging.list_staged(slug):
        rel = entry["path"]
        new_text, new_bytes = _read(safe_join(base, rel))
        old_text = _read(project / rel)[0] if (project / rel).is_file() else ""
        for f in scan(old_text, new_text, rel):
            current.add((rel, f["trigger"]))
            details[(rel, f["trigger"])] = f["detail"]
        for name in secrets_mod.find_in_bytes(new_bytes):
            current.add((rel, "secret_leak"))
            details[(rel, "secret_leak")] = {"secret": name}

    # existing rows for this project
    async with db.execute("SELECT id, path, trigger FROM gate_flags WHERE project_slug = ?",
                          (slug,)) as cur:
        existing = {(r["path"], r["trigger"]): r["id"] for r in await cur.fetchall()}

    # drop rows that no longer apply
    for key, rid in existing.items():
        if key not in current:
            await db.execute("DELETE FROM gate_flags WHERE id = ?", (rid,))
    # insert new trips + alert once
    import json
    for key in current - set(existing):
        rel, trig = key
        await db.execute(
            "INSERT OR IGNORE INTO gate_flags(project_slug, path, trigger, detail) VALUES (?,?,?,?)",
            (slug, rel, trig, json.dumps(details[key])))
        await security.raise_event(
            db, kind="gate_flag", severity="warn", project=slug,
            summary=f"diff gate: {trig} in {rel}", detail={"path": rel, "trigger": trig, **details[key]})
    await db.commit()
    return await list_flags(db, slug)


async def list_flags(db: aiosqlite.Connection, slug: str) -> list[dict]:
    import json
    async with db.execute(
            "SELECT id, path, trigger, detail, acknowledged, created_at FROM gate_flags "
            "WHERE project_slug = ? ORDER BY acknowledged, id", (slug,)) as cur:
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out


async def blocking_paths(db: aiosqlite.Connection, slug: str,
                         paths: list[str] | None = None) -> set[str]:
    """Staged paths that have >=1 UNACKNOWLEDGED flag (so approval is blocked)."""
    async with db.execute(
            "SELECT DISTINCT path FROM gate_flags WHERE project_slug = ? AND acknowledged = 0",
            (slug,)) as cur:
        blocked = {r["path"] for r in await cur.fetchall()}
    return blocked if paths is None else (blocked & set(paths))


async def acknowledge(db: aiosqlite.Connection, flag_id: int) -> dict:
    await db.execute("UPDATE gate_flags SET acknowledged=1, acknowledged_at=datetime('now') "
                     "WHERE id = ?", (flag_id,))
    await db.commit()
    return {"ok": True}
