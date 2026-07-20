import re

from backend.memory import notes_dir, parse_note
from backend.runtime import write_taint


def _safe_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not slug:
        raise ValueError("bad note name")
    return slug


def _with_frontmatter(description: str | None, body: str, taint: str | None = None) -> str:
    # Every note this tool writes is agent-authored, so it is stamped untrusted:
    # memory.note_trusted() keeps it out of the binding system prompt until the
    # operator approves it (flips approved: true). This is what stops laundered
    # web content from being promoted to a standing rule by writing it to memory.
    lines = ["source: agent", "approved: false"]
    if description:
        # single-line YAML value; a stray colon/quote must not break parsing
        lines.append(f"description: {' '.join(description.split())!r}")
    # taint (persisted): set when the write happened in a turn that had already
    # consumed untrusted external content, or carried forward from a prior write.
    # It is STICKY — only the operator's promote action clears it. This survives
    # append/replace, which the old frontmatter writer silently dropped.
    if taint:
        lines.append(f"taint: {taint}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body.rstrip() + "\n"


async def run(name: str, content: str, mode: str = "append",
              description: str | None = None) -> str:
    notes = notes_dir()
    notes.mkdir(parents=True, exist_ok=True)
    path = notes / f"{_safe_name(name)}.md"
    if mode == "delete":
        if not path.exists():
            return (f"error: no note named '{name}' to delete — "
                    "list notes with memory_read first")
        path.unlink()
        return f"memory note '{path.stem}' deleted"
    op_taint = write_taint.get()
    if mode == "replace" or not path.exists():
        # taint is STICKY: a clean-turn replace of an already-tainted note keeps
        # the untrusted provenance (only the operator's promote clears it).
        prior = None
        if path.exists():
            try:
                prior = parse_note(path.read_text())[0].get("taint")
            except OSError:
                pass
        path.write_text(_with_frontmatter(description, content, taint=op_taint or prior))
        return f"memory note '{path.stem}' written"
    # append: keep (or update) the existing frontmatter, never duplicate it, and
    # carry the taint forward (a new untrusted write escalates a clean note).
    meta, body = parse_note(path.read_text())
    desc = description or meta.get("description")
    taint = op_taint or meta.get("taint")
    path.write_text(_with_frontmatter(desc, body + "\n\n" + content.strip(), taint=taint))
    return f"appended to memory note '{path.stem}'"
