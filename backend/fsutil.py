from pathlib import Path

from fastapi import HTTPException

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist"}

# What file listings hide. dist/ is build output the operator and the renderer
# need to see (a built game failed to open 4 times: dist/ was hidden); it stays
# out of the code index via SKIP_DIRS.
LIST_SKIP_DIRS = SKIP_DIRS - {"dist"}


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


# With dotfiles shown a listing still never carries the harness's own files,
# nor the caches tools leave next to a project's real dotfiles.
HARNESS_HIDDEN = {".git", ".staging", ".workspace.json", ".context.json"}
CACHE_HIDDEN = {".cache", ".npm", ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".next",
                ".nuxt", ".parcel-cache", ".turbo", ".svelte-kit", ".gradle", ".idea",
                ".DS_Store"}


def list_tree(base: Path, dotfiles: bool = False) -> list[dict]:
    """All files under base (relative paths), skipping junk dirs. Dotfiles and
    dot-dirs are left out unless `dotfiles`: the operator's listings hide them,
    the workspace copy the guest works on shows them (a .gitignore, .eslintrc,
    .github/ are part of the project). The guest's copy of this function
    defaults the other way."""
    out = []
    if not base.exists():
        return out
    for p in sorted(base.rglob("*")):
        if p.is_dir():
            continue
        parts = p.relative_to(base).parts
        if dotfiles:
            if any(part in LIST_SKIP_DIRS or part in HARNESS_HIDDEN or part in CACHE_HIDDEN
                   for part in parts):
                continue
        else:
            if any(part in LIST_SKIP_DIRS or part.startswith(".") for part in parts[:-1]):
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
