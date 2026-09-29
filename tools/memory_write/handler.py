import re

import yaml

from backend import memory
from backend import secrets as secrets_mod
from backend.memory import notes_dir, parse_note, strip_leading_frontmatter
from backend.memory import weakening_advice
from backend.runtime import nav_taint, write_taint


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
    if description is not None and str(description).strip():
        description = str(description)   # a hand-edited note may hold an int or a date
        # single-line YAML value dumped BY yaml: a Python repr is not YAML (both
        # quote kinds made it unparseable, and an unparseable note used to read
        # as operator-authored and trusted)
        lines.append(yaml.safe_dump({"description": " ".join(description.split())},
                                    allow_unicode=True, width=1 << 20).strip())
    # taint (persisted): set when the write happened in a turn that had already
    # consumed untrusted external content, or carried forward from a prior write.
    # It is STICKY — only the operator's promote action clears it. This survives
    # append/replace, which the old frontmatter writer silently dropped.
    if taint:
        lines.append(f"taint: {taint}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body.rstrip() + "\n"


async def _refused_event(note: str, src: str, hit: str) -> None:
    try:
        from backend import security
        from backend.db import get_db
        db = await get_db()
        try:
            await security.raise_event(
                db, kind="memory_refused", severity="warn",
                summary=f"memory note '{note}' refused: written after reading a "
                        f"{'screen' if src == 'desk' else 'web page'}, it recommends "
                        "weakening a guard",
                detail={"note": note, "source": src, "match": hit[:120]})
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — the refusal stands even if the alert fails
        pass


_MODES = ("append", "replace", "delete")


def _label(stem: str, name: str) -> str:
    """The note as the model should name it from now on: when the name it gave
    was rewritten (case, spaces, punctuation, a path), say so, or it would go on
    calling the note by a name it never wrote."""
    if stem == name:
        return f"'{stem}'"
    return (f"'{stem}' (your name {name!r} was normalised; use '{stem}' with "
            "memory_read and memory_write)")


async def _delete(stem: str, name: str, notes, path) -> str:
    """Delete = move to the trash (the operator restores it from the Memory
    page), with an audit event. A binding note, one the operator wrote or
    approved, cannot be deleted from a turn that has read untrusted content:
    that would strip a standing rule on an injection's say-so."""
    if not path.exists() and not memory.proposal_path(stem, notes).exists():
        return (f"error: no note named '{name}' to delete — "
                "list notes with memory_read first")
    binding = False
    if path.exists():
        try:
            binding = memory.note_trusted(parse_note(path.read_text())[0])
        except OSError:
            pass
    if binding and write_taint.get():
        await memory.audit("memory_refused", "warn",
                           f"memory note '{stem}' delete refused: this turn had read "
                           "untrusted content and the note is binding",
                           {"note": stem, "by": "agent"})
        return ("error: refused — this turn read untrusted content (a web page, a "
                f"search, a file or a message), and '{stem}' is one of the operator's "
                "binding notes. Tell the operator what you wanted removed; they can "
                "delete it on the Memory page.")
    tid = memory.trash_note(stem, notes)
    await memory.audit("memory_deleted", "warn" if binding else "info",
                       f"memory note '{stem}' deleted by an agent"
                       + (" (it was binding)" if binding else ""),
                       {"note": stem, "by": "agent", "trash_id": tid, "binding": binding})
    return (f"memory note '{stem}' deleted (moved to the trash; the operator can "
            "restore it from the Memory page)")


async def run(name: str, content: str, mode: str | None = "append",
              description: str | None = None) -> str:
    mode = "append" if mode is None else str(mode).strip().lower()
    if mode not in _MODES:
        return (f"error: unknown mode {mode!r}. Use one of: append (add to the "
                "note), replace (rewrite it), delete (remove it).")
    name = str(name)
    try:
        stem = _safe_name(name)
    except ValueError:
        return "error: bad note name. Use letters, digits and hyphens."
    label = _label(stem, name)
    content = "" if content is None else str(content)
    description = None if description is None else str(description)
    notes = notes_dir()
    notes.mkdir(parents=True, exist_ok=True)
    path = notes / f"{stem}.md"
    # same hard line as writes.apply_write: a real secret VALUE never lands in
    # an agent-reachable file — memory notes are read back verbatim by
    # memory_read and would otherwise be an unscanned side door. The name and
    # the description are written into the file (and the prompt index) too.
    leaks = secrets_mod.find_in_bytes("\n".join((name, description or "", content)).encode())
    if leaks:
        return ("error: refused — the note contains the literal value of "
                f"an operator secret ({', '.join(leaks)}). Use the "
                "{{secret:NAME}} placeholder form instead.")
    if mode == "delete":
        return await _delete(stem, name, notes, path)
    # a leading --- block in the body is the model's own frontmatter, not ours
    body_desc, content = strip_leading_frontmatter(content)
    description = description or body_desc
    src = nav_taint.get()
    hit = weakening_advice(f"{description or ''}\n{content}") if src else None
    if hit:
        # refused, not quarantined: a screen or a page talked this turn into
        # recommending a weaker guard (the live trial's "turn shell on" note)
        await _refused_event(path.stem, src, hit)
        return ("error: refused — this turn read a " + ("screen" if src == "desk" else "web page")
                + f" and the note recommends weakening a guard (\"{hit[:60]}\"). "
                "Tell the operator what you needed instead; changing Jav3's "
                "permissions is their call, made in Settings.")
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
        return f"memory note {label} written"
    # append: keep (or update) the existing frontmatter, never duplicate it, and
    # carry the taint forward (a new untrusted write escalates a clean note).
    meta, body = parse_note(path.read_text())
    desc = description or meta.get("description")
    taint = op_taint or meta.get("taint")
    path.write_text(_with_frontmatter(desc, body + "\n\n" + content.strip(), taint=taint))
    return f"appended to memory note {label}"
