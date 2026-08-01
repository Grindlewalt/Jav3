from pathlib import Path

from fastapi import HTTPException

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist"}


def safe_join(base: Path, rel: str) -> Path:
    """Resolve rel against base, refusing anything that escapes base."""
    p = (base / rel).resolve()
    if not p.is_relative_to(base.resolve()):
        raise HTTPException(status_code=400, detail="path escapes base directory")
    return p


def find_file(base: Path, wanted: str, only=None) -> tuple[str | None, list[str]]:
    """Resolve what the model said to a real project file.

    Returns (relative path, candidates). A path is returned when it is
    unambiguous; otherwise candidates is what to show instead of guessing.

    This exists because of one recurring failure. A tool writes
    `dashboards/weather-report.html` and says so, and the next call asks to open
    `weather-report.html` — the bare name, which is what a person would say and
    what the model remembers. That is not a wrong answer, it is an
    under-specified one, and the old behaviour ("no such file") sent the whole
    turn round again to re-derive a path it had already been told. Matching is a
    string problem, so it is done here rather than by the model.

    Order: exact hit, then a unique basename, then a unique path suffix
    ("dashboards/x.html" for "x.html" is the same file by either route).
    `only` filters candidates to a kind of file, so the renderer's miss list is
    the renderer's menu rather than every file in the project.
    """
    entries = [e["path"] for e in list_tree(base)]
    if only is not None:
        entries = [p for p in entries if only.search(p)]
    wanted = (wanted or "").strip().replace("\\", "/")
    while wanted.startswith("./"):      # only a literal "./" prefix, so a
        wanted = wanted[2:]             # ".." keeps its meaning and matches nothing
    if not wanted:
        return None, entries
    if wanted in entries:
        return wanted, entries
    name = wanted.rsplit("/", 1)[-1].lower()
    hits = [p for p in entries if p.rsplit("/", 1)[-1].lower() == name]
    if len(hits) == 1:
        return hits[0], entries
    if not hits:
        # a partial path: "reports/news.json" against "out/reports/news.json"
        tail = [p for p in entries if p.lower().endswith("/" + wanted.lower())]
        if len(tail) == 1:
            return tail[0], entries
    return None, (hits or entries)


def list_tree(base: Path) -> list[dict]:
    """All files under base (relative paths), skipping junk dirs."""
    out = []
    if not base.exists():
        return out
    for p in sorted(base.rglob("*")):
        if p.is_dir():
            continue
        parts = p.relative_to(base).parts
        if any(part in SKIP_DIRS or part.startswith(".") for part in parts[:-1]):
            continue
        if p.name.startswith(".") and p.name != ".gitkeep":
            continue
        stat = p.stat()
        out.append({
            "path": str(p.relative_to(base)),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    return out


def read_text_or_binary(path: Path) -> dict:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    data = path.read_bytes()
    try:
        return {"binary": False, "content": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"binary": True, "content": None, "size": len(data)}
