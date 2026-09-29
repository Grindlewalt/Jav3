import re

from backend.memory import (note_description, note_taint, note_trusted, notes_dir,
                            parse_note)


def _safe_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not slug:
        raise ValueError("bad note name")
    return slug


def _taint_turn() -> None:
    """The body just handed to the model is text derived from untrusted content
    (or from a file we could not read as a note): the turn is tainted exactly as
    a web_read would taint it, so a follow-up memory_write is stamped and
    quarantined instead of restating the text with clean provenance."""
    from backend.agent import budget as budget_mod
    from backend.vm import broker
    broker.mark_tainted(budget_mod.active_op_id.get())


def _list_line(stem: str, meta: dict, body: str) -> str:
    if note_taint(meta) == "untrusted":
        # its description is free text derived from untrusted content: name only,
        # the same rule the prompt's own index applies
        return f"{stem} [pending approval, from untrusted content: read it to see the text]"
    desc = note_description(meta, body)
    line = f"{stem} — {desc}" if desc else stem
    return line if note_trusted(meta) else f"{line} [pending approval]"


async def run(name: str | None = None) -> str:
    notes = notes_dir()
    if name is None:
        files = sorted(notes.glob("*.md")) if notes.exists() else []
        if not files:
            return "no memory notes yet"
        lines = []
        for p in files:
            meta, body = parse_note(p.read_text())
            lines.append(_list_line(p.stem, meta, body))
        return "\n".join(lines)
    path = notes / f"{_safe_name(name)}.md"
    if not path.exists():
        have = sorted(p.stem for p in notes.glob("*.md")) if notes.exists() else []
        hint = f" Available notes: {', '.join(have)}" if have else " There are no notes yet."
        return f"error: no note named '{name}'.{hint}"
    text = path.read_text()
    meta, _ = parse_note(text)
    if note_taint(meta) == "untrusted":
        _taint_turn()
    return text
