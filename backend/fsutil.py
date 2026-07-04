from pathlib import Path

from fastapi import HTTPException

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist"}


def safe_join(base: Path, rel: str) -> Path:
    """Resolve rel against base, refusing anything that escapes base."""
    p = (base / rel).resolve()
    if not p.is_relative_to(base.resolve()):
        raise HTTPException(status_code=400, detail="path escapes base directory")
    return p


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
