import yaml

from backend import memory
from backend import secrets as secrets_mod
from backend.memory import (note_slug, notes_dir, parse_note, resolve_note,
                            strip_leading_frontmatter)
from backend.memory import weakening_advice
from backend.runtime import nav_taint, write_taint


def _with_frontmatter(description: str | None, body: str, taint: str | None = None,
                      extra: dict | None = None) -> str:
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
    for k, v in (extra or {}).items():
        lines.append(yaml.safe_dump({k: str(v)}, width=1 << 20).strip())
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

# What every save of an agent's own note must tell the model. The note is stamped
# `approved: false`, so prompt assembly lists it by name only and it never reaches
# the rules; the agent used to be told "written" and then told the operator their
# preference was now in effect.
_PENDING = ("It is PENDING: not in your context, your index or your rules until "
            "the operator approves it on the Memory page. Tell them it is waiting; "
            "do not say the preference is in effect.")


def _saved(verb: str, label: str) -> str:
    """The result of a write that lands as the agent's own, pending, note."""
    from backend import runtime
    if runtime.ephemeral.get():
        # an incognito turn writes to a throwaway dir: nothing waits for approval
        return (f"{verb} {label} for this incognito chat only: it is gone when the "
                "chat ends and the operator never sees it.")
    return f"{verb} {label}. {_PENDING}"


def _label(stem: str, name: str) -> str:
    """The note as the model should name it from now on: when the name it gave
    was rewritten (case, spaces, punctuation, a path), say so, or it would go on
    calling the note by a name it never wrote."""
    if stem == name:
        return f"'{stem}'"
    return (f"'{stem}' (your name {name[:60]!r} was normalised; use '{stem}' with "
            "memory_read and memory_write)")


async def _propose(stem, label, mode, description, content, op_taint, notes,
                   base_text, base_meta, base_body) -> str:
    """An agent write onto a binding note. The note is NOT touched: rewriting it
    as `source: agent, approved: false` used to demote the operator's own text,
    so the standing rules in it dropped out of the prompt. The change is kept as
    a complete proposed note in .proposals/, which prompt assembly never reads;
    the operator reviews a diff (GET /api/memory/proposals) and approves or rejects it. More
    writes before then build on the proposal, not on the note."""
    if mode == "replace" and not content.strip():
        return ("error: the content is empty. To remove a note use mode=delete "
                "(it goes to the trash).")
    cur = memory.read_proposal(stem, notes)
    if mode == "replace":
        new_body = content.strip()
    else:
        new_body = ((cur["body"] if cur else base_body) + "\n\n" + content.strip()).strip()
    desc = (description or (cur and cur["meta"].get("description"))
            or base_meta.get("description"))
    taint = op_taint or (cur["meta"].get("taint") if cur else None)
    base_sha = (cur["meta"].get("base_sha256") if cur else None) or memory.sha256_text(base_text)
    p = memory.proposal_path(stem, notes)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_with_frontmatter(desc, new_body, taint=taint,
                                   extra={"proposal_for": stem, "base_sha256": base_sha}))
    if cur is None:
        memory.notify_pending(stem, proposal=True)
    await memory.audit("memory_proposed", "warn",
                       f"an agent proposed a change to note '{stem}'"
                       + (" (written after untrusted content)" if taint else ""),
                       {"note": stem, "mode": mode, "tainted": bool(taint)})
    return (f"note {label} is one the operator wrote or approved, so it is unchanged "
            "and still binding. Your change is saved as a proposal that takes effect "
            "only when the operator approves it on the Memory page. Tell them.")


async def _delete(stem: str, name: str, notes, path) -> str:
    """Delete = move to the trash (the operator can restore it: GET /api/memory/trash),
    with an audit event. A binding note, one the operator wrote or
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
                "binding notes. Tell the operator what you wanted removed; deleting it "
                "is their call.")
    tid = memory.trash_note(stem, notes)
    await memory.audit("memory_deleted", "warn" if binding else "info",
                       f"memory note '{stem}' deleted by an agent"
                       + (" (it was binding)" if binding else ""),
                       {"note": stem, "by": "agent", "trash_id": tid, "binding": binding})
    return (f"memory note '{stem}' deleted (moved to the trash; the operator can "
            "restore it)")


async def run(name: str, content: str, mode: str | None = "append",
              description: str | None = None) -> str:
    mode = "append" if mode is None else str(mode).strip().lower()
    if mode not in _MODES:
        return (f"error: unknown mode {mode!r}. Use one of: append (add to the "
                "note), replace (rewrite it), delete (remove it).")
    name = str(name)
    notes = notes_dir()
    try:
        # a note that exists is addressed by the name it has (the operator's
        # 'My Ideas.md'), a new one gets the plain slug
        stem = resolve_note(name, notes) or note_slug(name)
    except ValueError:
        return "error: bad note name. Use letters, digits and hyphens."
    label = _label(stem, name)
    content = "" if content is None else str(content)
    description = None if description is None else str(description)
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
    if path.exists():
        try:
            base_text = path.read_text()
        except OSError:
            base_text = None
        if base_text is not None:
            base_meta, base_body = parse_note(base_text)
            if memory.note_trusted(base_meta):
                # binding today (the operator wrote it, or approved it): it stays
                # exactly as it is, and the change waits for the operator
                return await _propose(stem, label, mode, description, content, op_taint,
                                      notes, base_text, base_meta, base_body)
    if mode == "replace" or not path.exists():
        # taint is STICKY: a clean-turn replace of an already-tainted note keeps
        # the untrusted provenance (only the operator's promote clears it).
        prior = None
        if path.exists():
            try:
                prior = parse_note(path.read_text())[0].get("taint")
            except OSError:
                pass
        was_pending = path.exists()
        path.write_text(_with_frontmatter(description, content, taint=op_taint or prior))
        if not was_pending:
            memory.notify_pending(stem)
        return _saved("memory note", f"{label} saved")
    # append: keep (or update) the existing frontmatter, never duplicate it, and
    # carry the taint forward (a new untrusted write escalates a clean note).
    meta, body = parse_note(path.read_text())
    desc = description or meta.get("description")
    taint = op_taint or meta.get("taint")
    path.write_text(_with_frontmatter(desc, body + "\n\n" + content.strip(), taint=taint))
    return _saved("appended to memory note", label)
