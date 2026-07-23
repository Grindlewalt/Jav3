"""Deterministic diff gates.

Grep/heuristic checks over a file write's diff — NO model in the loop, because
an AI reviewing AI output is not a control. Since the staging quarantine was
removed (2026-07-19) a trip is ADVISORY: the write lands on the canonical file
and `writes.apply_write` raises a deduped security event (Review Center + bell)
per trigger. secret_leak is the exception — writes.py refuses those outright.

Triggers the operator chose (large-unrelated-diff deliberately dropped as noise):
  new_import        — an added import / dependency (supply-chain injection)
  network_call      — an added outbound-call primitive (exfil / C2 / beacon)
  high_entropy      — an added base64/hex blob (embedded payload / obfuscation)
  logging_removed   — fewer logging calls than before (evasion)
  assertion_removed — fewer assertions than before (test tampering)

`scan()` is pure and fully unit-testable.
"""
import math
import re

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
    """Deterministic flags for one file write's old->new diff. `old_text`
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
