import re

from backend.memory import note_description, notes_dir, parse_note


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
        lines = []
        for p in files:
            meta, body = parse_note(p.read_text())
            desc = note_description(meta, body)
            lines.append(f"{p.stem} — {desc}" if desc else p.stem)
        return "\n".join(lines)
    path = notes / f"{_safe_name(name)}.md"
    if not path.exists():
        have = sorted(p.stem for p in notes.glob("*.md")) if notes.exists() else []
        hint = f" Available notes: {', '.join(have)}" if have else " There are no notes yet."
        return f"error: no note named '{name}'.{hint}"
    return path.read_text()
