import re

from backend.memory import notes_dir


def _safe_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not slug:
        raise ValueError("bad note name")
    return slug


async def run(name: str | None = None) -> str:
    notes = notes_dir()
    if name is None:
        files = sorted(notes.glob("*.md")) if notes.exists() else []
        if not files:
            return "no memory notes yet"
        return "\n".join(f"{p.stem}  [{p.stat().st_size}B]" for p in files)
    path = notes / f"{_safe_name(name)}.md"
    if not path.exists():
        return f"error: no note named '{name}'"
    return path.read_text()
