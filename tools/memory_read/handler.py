import re

from backend.memory import (note_description, note_taint, note_trusted, notes_dir,
                            parse_note, proposal_path, read_proposal)


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


def _list_line(stem: str, meta: dict, body: str, notes) -> str:
    if note_taint(meta) == "untrusted":
        # its description is free text derived from untrusted content: name only,
        # the same rule the prompt's own index applies
        return f"{stem} [pending approval, from untrusted content: read it to see the text]"
    desc = note_description(meta, body)
    line = f"{stem} — {desc}" if desc else stem
    if not note_trusted(meta):
        return f"{line} [pending approval]"
    return f"{line} [change pending approval]" if proposal_path(stem, notes).is_file() else line


async def run(name: str | None = None) -> str:
    notes = notes_dir()
    if name is None:
        files = sorted(notes.glob("*.md")) if notes.exists() else []
        if not files:
            return "no memory notes yet"
        lines = []
        for p in files:
            meta, body = parse_note(p.read_text())
            lines.append(_list_line(p.stem, meta, body, notes))
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
    prop = read_proposal(path.stem, notes)
    if prop is not None:
        # a change an agent (maybe this one) proposed; the note above is what
        # is binding. Its text can come from untrusted content like any other.
        if note_taint(prop["meta"]) == "untrusted":
            _taint_turn()
        text += ("\n\n[A change to this note is pending the operator's approval. It is NOT "
                 "binding yet. The proposed note:]\n" + prop["body"] + "\n")
    return text
