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

Each flag carries `lines`: the 1-based line numbers in the NEW text that
tripped it, so the Review Center can show the operator the actual code instead
of a bag of matched substrings (see `secctx.py`). Removal triggers have no
location — nothing was added to point at — so they omit the key.

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


def _added_lines(old: str, new: str) -> list[tuple[int, str]]:
    """(1-based line number in `new`, text) for every line not already in old."""
    old_set = {ln.strip() for ln in old.splitlines()}
    return [(i, ln) for i, ln in enumerate(new.splitlines(), 1)
            if ln.strip() and ln.strip() not in old_set]


def _imports(lines: list[tuple[int, str]]) -> dict[str, list[int]]:
    """module -> the added line numbers that import it."""
    mods: dict[str, list[int]] = {}
    for n, ln in lines:
        m = _PY_IMPORT.match(ln)
        if m:
            mods.setdefault((m.group(1) or m.group(2)).split(".")[0], []).append(n)
        for jm in _JS_IMPORT.finditer(ln):
            mods.setdefault(jm.group(1) or jm.group(2), []).append(n)
    return {m: ns for m, ns in mods.items() if m}


def _count(rx: re.Pattern, text: str) -> int:
    return len(rx.findall(text))


# a flag points at a bounded number of lines; the board shows a snippet per
# line and a hundred of them is a wall, not evidence
_MAX_LINES = 40


def _lines(groups) -> list[int]:
    out: set[int] = set()
    for g in groups:
        out.update(g)
    return sorted(out)[:_MAX_LINES]


def scan(old_text: str, new_text: str, path: str) -> list[dict]:
    """Deterministic flags for one file write's old->new diff. `old_text`
    is '' for a brand-new file. Returns [{trigger, detail}]."""
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext not in _CODE_EXT and not path.endswith("Dockerfile"):
        return []
    added = _added_lines(old_text, new_text)
    flags: list[dict] = []

    new_mods = _imports(added)
    if new_mods:
        flags.append({"trigger": "new_import",
                      "detail": {"modules": sorted(new_mods),
                                 "lines": _lines(new_mods.values())}})

    net: dict[str, list[int]] = {}
    for n, ln in added:
        for m in _NET.finditer(ln):
            net.setdefault(m.group(0), []).append(n)
    if net:
        flags.append({"trigger": "network_call",
                      "detail": {"matches": sorted(net)[:8],
                                 "lines": _lines(net.values())}})

    blobs: list[tuple[int, str]] = []
    for n, ln in added:
        blobs += [(n, b) for b in _B64.findall(ln) if _shannon(b) >= 4.0]
        blobs += [(n, h) for h in _HEX.findall(ln)]
    if blobs:
        flags.append({"trigger": "high_entropy",
                      "detail": {"count": len(blobs), "sample": blobs[0][1][:24] + "…",
                                 "lines": _lines([[n for n, _ in blobs]])}})

    if _count(_LOG, old_text) > _count(_LOG, new_text):
        flags.append({"trigger": "logging_removed",
                      "detail": {"before": _count(_LOG, old_text), "after": _count(_LOG, new_text)}})

    if _count(_ASSERT, old_text) > _count(_ASSERT, new_text):
        flags.append({"trigger": "assertion_removed",
                      "detail": {"before": _count(_ASSERT, old_text), "after": _count(_ASSERT, new_text)}})
    return flags


def locate(text: str, trigger: str, detail: dict) -> list[int]:
    """The 1-based lines of `text` that would trip `trigger` now.

    `scan` records line numbers at write time, but the file may have been
    rewritten since (or the event may pre-date the recording), and stale numbers
    point at bytes that are no longer there. The review board re-finds the lines
    with this; the patterns live here so there is one definition of what each
    trigger means.
    """
    lines = text.splitlines()
    hits: set[int] = set()
    if trigger == "new_import":
        want = {str(m) for m in (detail.get("modules") or [])}
        for i, ln in enumerate(lines, 1):
            m = _PY_IMPORT.match(ln)
            if m and (m.group(1) or m.group(2)).split(".")[0] in want:
                hits.add(i)
                continue
            if any((jm.group(1) or jm.group(2)) in want
                   for jm in _JS_IMPORT.finditer(ln)):
                hits.add(i)
    elif trigger == "network_call":
        hits = {i for i, ln in enumerate(lines, 1) if _NET.search(ln)}
    elif trigger == "high_entropy":
        for i, ln in enumerate(lines, 1):
            if _HEX.search(ln) or any(_shannon(b) >= 4.0 for b in _B64.findall(ln)):
                hits.add(i)
    return sorted(hits)[:_MAX_LINES]
