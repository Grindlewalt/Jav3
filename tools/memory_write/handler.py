import re

from backend.memory import notes_dir


def _safe_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not slug:
        raise ValueError("bad note name")
    return slug


async def run(name: str, content: str, mode: str = "append") -> str:
    notes = notes_dir()
    notes.mkdir(parents=True, exist_ok=True)
    path = notes / f"{_safe_name(name)}.md"
    if mode == "replace" or not path.exists():
        path.write_text(content.rstrip() + "\n")
        return f"memory note '{path.stem}' written"
    with path.open("a") as f:
        f.write("\n" + content.rstrip() + "\n")
    return f"appended to memory note '{path.stem}'"
