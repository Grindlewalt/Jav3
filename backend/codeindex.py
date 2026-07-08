"""Deterministic codebase indexer + search. No LLM calls.

build_index walks an uploaded repo and writes notes/codebase/ — INDEX.md plus
one detail file per top-level directory. These notes are DERIVED data written
canonically (not staged): the index is a pure function of files already in the
project, so there is nothing for the operator to review.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .fsutil import SKIP_DIRS, safe_join

MAX_FILE_BYTES = 512 * 1024
INDEX_MAX_ENTRIES = 1500
NOTES_SUBDIR = "notes/codebase"

_PY = re.compile(r"^(?:async\s+)?(def|class)\s+(\w+)")
_JS = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?"
                 r"(?:(function|class)\s+(\w+)|const\s+(\w+)\s*=)")
_GO = re.compile(r"^(func|type)\s+(?:\([^)]*\)\s*)?(\w+)")
_COMMENT = re.compile(r"^\s*(?:#|//|/\*+|\*|--|\"\"\"|''')\s*(.+)")

_JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}


def _symbols(suffix: str, text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if suffix == ".py":
            m = _PY.match(line)
            if m:
                out.append(f"{m.group(1)} {m.group(2)}")
        elif suffix in _JS_EXTS:
            m = _JS.match(line)
            if m:
                if m.group(1):
                    out.append(f"{m.group(1)} {m.group(2)}")
                elif line.lstrip().startswith("export"):
                    out.append(f"const {m.group(3)}")
        elif suffix == ".go":
            m = _GO.match(line)
            if m:
                out.append(f"{m.group(1)} {m.group(2)}")
    if not out:  # generic fallback: first comment/docstring line
        for line in text.splitlines()[:10]:
            m = _COMMENT.match(line)
            if m and m.group(1).strip("*/- "):
                out.append(m.group(1).strip("*/- ")[:80])
                break
    return out


def _walk(root: Path) -> list[Path]:
    """Text files under root, skipping junk/dot dirs, binaries and huge files."""
    files = []
    if not root.is_dir():
        return files
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        parts = p.relative_to(root).parts
        if any(part in SKIP_DIRS or part.startswith(".") for part in parts):
            continue
        if p.stat().st_size > MAX_FILE_BYTES:
            continue
        if b"\x00" in p.read_bytes()[:8192]:
            continue
        files.append(p)
    return files


def build_index(slug: str, subdir: str = "code") -> dict:
    base = settings.projects_dir / slug
    root = safe_join(base, subdir) if subdir else base
    files = _walk(root)
    notes_dir = base / NOTES_SUBDIR
    notes_dir.mkdir(parents=True, exist_ok=True)

    entries = []  # (relpath, lines, size, symbols)
    total = 0
    for p in files:
        rel = str(p.relative_to(root))
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        size = p.stat().st_size
        total += size
        entries.append((rel, len(text.splitlines()), size,
                        _symbols(p.suffix, text)))

    by_dir: dict[str, list] = {}
    for e in entries:
        by_dir.setdefault(str(Path(e[0]).parent), []).append(e)
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = ["# Codebase index", "",
             f"built: {when}", f"root: {subdir or '.'}",
             f"files: {len(entries)}  dirs: {len(by_dir)}  total: {total} bytes", ""]
    shown = 0
    for d in sorted(by_dir):
        lines.append(f"## {d}/" if d != "." else "## (root)")
        for rel, n_lines, _size, syms in by_dir[d]:
            if shown >= INDEX_MAX_ENTRIES:
                break
            head = ", ".join(syms[:3])
            lines.append(f"{rel} ({n_lines} lines)" + (f" — {head}" if head else ""))
            shown += 1
        lines.append("")
        if shown >= INDEX_MAX_ENTRIES:
            lines.append(f"...(truncated at {INDEX_MAX_ENTRIES} files)")
            break
    (notes_dir / "INDEX.md").write_text("\n".join(lines) + "\n")

    # per-top-level-dir detail, so each note stays individually small
    by_top: dict[str, list] = {}
    for e in entries:
        parts = Path(e[0]).parts
        top = parts[0] if len(parts) > 1 else "_root"
        by_top.setdefault(top, []).append(e)
    for top, group in by_top.items():
        out = [f"# {subdir or '.'}/{top}" if top != "_root" else f"# {subdir or '.'} (root files)", ""]
        for rel, n_lines, size, syms in group:
            out.append(f"## {rel} ({n_lines} lines, {size} B)")
            out += [f"- {s}" for s in syms] or ["- (no symbols found)"]
            out.append("")
        (notes_dir / f"{top}.md").write_text("\n".join(out) + "\n")

    return {"files": len(entries), "dirs": len(by_dir), "bytes": total,
            "notes": [f"{NOTES_SUBDIR}/INDEX.md"]
                     + sorted(f"{NOTES_SUBDIR}/{t}.md" for t in by_top)}


def search_code(slug: str, query: str, subdir: str | None = None,
                regex: bool = False, max_results: int = 100,
                context: int = 0) -> list[dict]:
    """Case-insensitive scan; returns {path, line_no, line} hits, plus
    `before`/`after` neighbour lines when context > 0 — a bare match line
    usually forces a full-file read, a line of context usually doesn't."""
    base = settings.projects_dir / slug
    if regex:
        try:
            pat = re.compile(query, re.I)
        except re.error as e:
            raise ValueError(f"bad regex: {e}")
    else:
        pat = None
        needle = query.lower()

    roots = [safe_join(base, subdir), base / "notes"] if subdir else [base]
    seen: set[Path] = set()
    hits: list[dict] = []
    for root in roots:
        for p in _walk(root):
            if p in seen:
                continue
            seen.add(p)
            rel = str(p.relative_to(base))
            lines = p.read_text(errors="replace").splitlines()
            for i, line in enumerate(lines, 1):
                if (pat.search(line) if pat else needle in line.lower()):
                    hit = {"path": rel, "line_no": i, "line": line.strip()[:200]}
                    if context:
                        hit["before"] = [lines[j].strip()[:200]
                                         for j in range(max(0, i - 1 - context), i - 1)]
                        hit["after"] = [lines[j].strip()[:200]
                                        for j in range(i, min(len(lines), i + context))]
                    hits.append(hit)
                    if len(hits) >= max_results:
                        return hits
    return hits


def index_stale(slug: str, subdir: str = "code") -> int:
    """How many indexed-scope files changed since notes/codebase/ was built.
    0 = fresh, or no index exists yet (nothing to be stale)."""
    base = settings.projects_dir / slug
    index = base / NOTES_SUBDIR / "INDEX.md"
    if not index.is_file():
        return 0
    built = index.stat().st_mtime
    try:
        root = safe_join(base, subdir) if subdir else base
    except ValueError:
        return 0
    return sum(1 for p in _walk(root) if p.stat().st_mtime > built)
