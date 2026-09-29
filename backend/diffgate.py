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

new_import counts a module only when it could come from outside the project:
not a relative import (`./x.js`, `from . import x`), not the language's own
standard library or Node's builtins, EXCEPT the standard modules that open
sockets or run programs (socket, subprocess, http, child_process, net, ...),
which always count. The Python pattern runs on Python files only: on a .mjs
file it read `import test from 'node:test'` as a module named `test`. On the Pi
(2026-09-27) 242 new_import flags were almost all relative, stdlib or builtin.

`scan()` is pure and fully unit-testable.
"""
import math
import re
import sys

_PY_IMPORT = re.compile(r'^\s*(?:import\s+([\w.]+)|from\s+([\w.]+)\s+import)')
_JS_IMPORT = re.compile(r'''import\s.+from\s+["']([^"']+)["']|require\(\s*["']([^"']+)["']''')
_PY_EXT = {".py"}
_JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

# the standard library is not a supply chain, but these reach the network,
# run programs or load code by name, so adding one is still worth a flag
_PY_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))
_PY_SENSITIVE = frozenset({
    "socket", "ssl", "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib",
    "telnetlib", "xmlrpc", "socketserver", "subprocess", "ctypes", "pty",
    "webbrowser", "importlib", "runpy", "code", "codeop", "marshal"})
_NODE_BUILTINS = frozenset({
    "assert", "assert/strict", "async_hooks", "buffer", "child_process", "cluster",
    "console", "constants", "crypto", "dgram", "diagnostics_channel", "dns",
    "dns/promises", "domain", "events", "fs", "fs/promises", "http", "http2", "https",
    "inspector", "module", "net", "os", "path", "path/posix", "path/win32",
    "perf_hooks", "process", "punycode", "querystring", "readline",
    "readline/promises", "repl", "sea", "sqlite", "stream", "stream/consumers",
    "stream/promises", "stream/web", "string_decoder", "sys", "test", "test/reporters",
    "timers", "timers/promises", "tls", "trace_events", "tty", "url", "util",
    "util/types", "v8", "vm", "wasi", "worker_threads", "zlib"})
_NODE_SENSITIVE = frozenset({
    "child_process", "cluster", "dgram", "dns", "dns/promises", "http", "http2",
    "https", "inspector", "module", "net", "tls", "vm", "worker_threads"})


def _external_py(mod: str) -> bool:
    """A top-level Python module name that may come from outside the project."""
    if not mod:                          # `from . import x` / `from .m import x`
        return False
    return mod not in _PY_STDLIB or mod in _PY_SENSITIVE


def _external_js(spec: str) -> bool:
    """A JS import specifier that may come from outside the project."""
    if not spec or spec.startswith((".", "/")):
        return False
    name = spec[5:] if spec.startswith("node:") else spec
    if name in _NODE_BUILTINS:
        return name in _NODE_SENSITIVE
    return True
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


def _imports(lines: list[tuple[int, str]], ext: str = "") -> dict[str, list[int]]:
    """module -> the added line numbers that import it, for modules that may
    come from outside the project. A file that is neither Python nor JS/TS
    (shell, YAML, ...) keeps the old behaviour: both patterns, no filtering."""
    py = ext in _PY_EXT or ext not in _JS_EXT
    js = ext in _JS_EXT or ext not in _PY_EXT
    known = ext in _PY_EXT or ext in _JS_EXT
    mods: dict[str, list[int]] = {}
    for n, ln in lines:
        m = _PY_IMPORT.match(ln) if py else None
        if m:
            top = (m.group(1) or m.group(2)).split(".")[0]
            if not known or _external_py(top):
                mods.setdefault(top, []).append(n)
        if js:
            for jm in _JS_IMPORT.finditer(ln):
                spec = jm.group(1) or jm.group(2)
                if not known or _external_js(spec):
                    mods.setdefault(spec, []).append(n)
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

    new_mods = _imports(added, ext)
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
