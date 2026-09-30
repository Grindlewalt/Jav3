"""GUI access to Jav3's memory files: list, read, edit, create notes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import alwaysloaded
from .auth import require_user
from .config import settings
from .fsutil import list_tree, read_text_or_binary, safe_join
from .memory import (NoteChanged, ProposalChanged, ProposalStale, approve_proposal, audit,
                     ensure_memory_seeds, estimate_tokens, list_proposals, list_trash,
                     note_description, note_taint, note_trusted, notes_dir, parse_note,
                     pending_counts, promote_note, proposal_path, proposal_view,
                     reject_proposal, restore_trash, sha256_text, trash_note)
from .writes import SecretLeakError

router = APIRouter(prefix="/api/memory", tags=["memory"],
                   dependencies=[Depends(require_user)])

# Regenerated from project summaries — hand edits get overwritten.
AUTO_GENERATED = {"all-projects.md"}


class SaveFile(BaseModel):
    path: str
    content: str
    # the sha256 of the text the editor loaded: a save over a file that is
    # different now (the agent appended, a scheduled run wrote) is a 409, not a
    # silent last-write-wins
    if_sha256: str | None = None
    # a new note must not replace one that exists
    create_only: bool = False


@router.get("")
async def list_memory():
    ensure_memory_seeds()
    files = list_tree(settings.memory_dir)
    for f in files:
        f["auto_generated"] = f["path"] in AUTO_GENERATED
        try:  # ≈input-token cost of the file if it rides the context
            f["tokens"] = estimate_tokens(
                (settings.memory_dir / f["path"]).read_text())
        except (UnicodeDecodeError, OSError):
            f["tokens"] = None
    return {"files": files}


@router.get("/file")
async def read_memory(path: str):
    p = safe_join(settings.memory_dir, path)
    r = read_text_or_binary(p)
    if not r.get("binary"):
        r["sha256"] = sha256_text(r["content"])
    return {"path": path, **r}


@router.put("/file")
async def save_memory(body: SaveFile):
    p = safe_join(settings.memory_dir, body.path)
    if body.create_only and p.exists():
        raise HTTPException(status_code=409, detail="a note with that name already exists")
    if body.if_sha256 and p.is_file():
        try:
            now = sha256_text(p.read_text())
        except (UnicodeDecodeError, OSError):
            now = None
        if now != body.if_sha256:
            raise HTTPException(
                status_code=409,
                detail="this file changed since you opened it (the agent may have written "
                       "to it); reload it before saving")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body.content)
    return {"ok": True, "path": body.path, "sha256": sha256_text(body.content)}


@router.get("/notes")
async def list_notes():
    """Notes with their trust/taint metadata — the Memory page's review queue and
    badges. A note that is not binding (`pending`) also carries its body, so the
    operator approves the text they read, bound to `sha256`."""
    nd = notes_dir()
    out = []
    if nd.exists():
        for p in sorted(nd.glob("*.md")):
            try:
                text = p.read_text()
                st = p.stat()
            except OSError:
                continue
            meta, body = parse_note(text)
            trusted = note_trusted(meta)
            row = {"name": p.stem,
                   "description": note_description(meta, body),
                   "source": str(meta.get("source", "operator")),
                   "approved": bool(meta.get("approved")),
                   "taint": note_taint(meta),
                   "trusted": trusted,
                   "pending": not trusted,
                   # frontmatter that would not parse: read as untrusted until repaired
                   "bad_frontmatter": bool(meta.get("_bad_frontmatter")),
                   "sha256": sha256_text(text),
                   "size": st.st_size, "mtime": st.st_mtime,
                   # an agent's change waiting for review (GET /proposals)
                   "proposal": proposal_path(p.stem, nd).is_file()}
            if not trusted:
                row["body"] = body[:20000]
            out.append(row)
    return {"notes": out}


@router.get("/pending")
async def pending():
    """How many things wait on the operator here: agent notes not yet approved
    plus agent changes proposed to binding notes."""
    return pending_counts()


class Promote(BaseModel):
    sha256: str | None = None


@router.post("/notes/{name}/promote")
async def promote(name: str, body: Promote | None = None):
    """Operator clears an agent/tainted note into trusted binding context. With
    `sha256` (what the page showed) a note that changed since answers 409."""
    name = _check_name(name)
    try:
        ok = promote_note(name, sha256=(body.sha256 if body else None))
    except NoteChanged:
        raise HTTPException(
            status_code=409,
            detail="the note changed since you opened it (the agent wrote to it); "
                   "reload it and read it again before approving") from None
    if not ok:
        raise HTTPException(status_code=404, detail="no such note")
    return {"ok": True, "name": name}


def _check_name(name: str) -> str:
    """A note name from a URL: one path segment, not hidden. The trash and the
    proposals live in dot-directories and are reached only through their own
    routes."""
    if (not name or name != name.strip() or name.startswith(".")
            or "/" in name or "\\" in name or "\x00" in name):
        raise HTTPException(status_code=400, detail="bad note name")
    return name


@router.delete("/notes/{name}")
async def delete_note(name: str):
    """Operator deletes a note. It goes to the trash, not away: restore it from
    GET /trash. (Agents delete the same way, with an audit event.)"""
    name = _check_name(name)
    try:
        tid = trash_note(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no such note") from None
    await audit("memory_deleted", "info", f"memory note '{name}' deleted by the operator",
                {"note": name, "by": "operator", "trash_id": tid})
    return {"ok": True, "name": name, "trash_id": tid}


class Approve(BaseModel):
    # the sha256 the page showed: approving binds to the text the operator read
    sha256: str | None = None
    # apply over a note that was edited after the proposal began
    force: bool = False


@router.get("/proposals")
async def proposals():
    """Agent changes to notes that are binding, waiting for review. Each carries
    a diff against the note as it is now."""
    return {"items": list_proposals()}


@router.get("/proposals/{name}")
async def proposal(name: str):
    view = proposal_view(_check_name(name))
    if view is None:
        raise HTTPException(status_code=404, detail="no proposal for that note")
    return view


@router.post("/proposals/{name}/approve")
async def approve(name: str, body: Approve | None = None):
    body = body or Approve()
    name = _check_name(name)
    try:
        approve_proposal(name, sha256=body.sha256, force=body.force)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no proposal for that note") from None
    except ProposalChanged:
        raise HTTPException(
            status_code=409,
            detail="the proposal changed since you opened it (the agent wrote again); "
                   "reload and review it") from None
    except ProposalStale:
        raise HTTPException(
            status_code=409,
            detail="the note was edited after this proposal began; the approval would "
                   "overwrite that edit. Reject it, or approve with force") from None
    await audit("memory_approved", "info", f"proposed change to note '{name}' approved",
                {"note": name, "by": "operator"})
    return {"ok": True, "name": name}


@router.post("/proposals/{name}/reject")
async def reject(name: str):
    if not reject_proposal(_check_name(name)):
        raise HTTPException(status_code=404, detail="no proposal for that note")
    return {"ok": True, "name": name}


# --- project files that ride every prompt (backend/alwaysloaded.py) -----------
# A write to project.md or an operator-ticked context file by a turn that had
# read untrusted content is held here; the prompt keeps the file as it was until
# the operator approves the change (a diff, bound to the text they read).

@router.get("/held-files")
async def held_files(project: str | None = None):
    """Held changes to always-loaded project files, all projects or one, each
    with a diff against the file as it is now."""
    return {"items": alwaysloaded.list_held(project)}


@router.post("/held-files/{slug}/{item_id}/approve")
async def approve_held(slug: str, item_id: str, body: Approve | None = None):
    body = body or Approve()
    try:
        path = await alwaysloaded.approve(slug, item_id, sha256=body.sha256, force=body.force)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no held change with that id") from None
    except alwaysloaded.HeldChanged:
        raise HTTPException(
            status_code=409,
            detail="the held change changed since you opened it (the agent wrote again); "
                   "reload and review it") from None
    except alwaysloaded.HeldStale:
        raise HTTPException(
            status_code=409,
            detail="the file was changed after this change was held; the approval would "
                   "overwrite that. Reject it, or approve with force") from None
    except SecretLeakError as e:
        raise HTTPException(
            status_code=422,
            detail=f"the change contains the value of secret(s) {', '.join(e.names)}; "
                   "it cannot be written") from None
    return {"ok": True, "project": slug, "path": path}


@router.post("/held-files/{slug}/{item_id}/reject")
async def reject_held(slug: str, item_id: str):
    path = await alwaysloaded.reject(slug, item_id)
    if path is None:
        raise HTTPException(status_code=404, detail="no held change with that id")
    return {"ok": True, "project": slug, "path": path}


@router.get("/trash")
async def trash():
    return {"items": list_trash()}


@router.post("/trash/{tid}/restore")
async def restore(tid: str):
    try:
        name = restore_trash(tid)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad trash id") from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not in the trash") from None
    except FileExistsError:
        raise HTTPException(
            status_code=409,
            detail="a note with that name exists now; rename or delete it first") from None
    return {"ok": True, "name": name}
