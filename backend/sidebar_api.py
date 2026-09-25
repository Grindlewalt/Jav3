"""GET /api/sidebar — everything the shell's sidebar draws, in one round-trip.

The terminal-style shell (`/shell`) opens on a sidebar that used to need four
requests: the conversation list, the folders, the projects and the approval
queues. This is those, read in one connection, plus the one thing none of them
answers alone — **who needs the operator right now**.

`needs` is keyed by scope, because approvals are: a git commit request and an
egress host both belong to a project slug (or to a project-less chat's hidden
`chat-<id>` store), never to a conversation. Each scope with something waiting
is attributed to its most recently active chat — the one most likely to have
raised it — so the sidebar can put that chat under "Needs you" and open it.
A scope with no chat at all (a plan blocked on a project nobody is talking
to) still appears, with `conversation_id: null`, and opens the project.

`running` is every chat-kind conversation with a live turn; the sidebar's
"Working" list is `running` minus whatever is already under "Needs you".
"""
from fastapi import APIRouter, Depends

from . import chat, plan
from .auth import require_user
from .db import get_db

router = APIRouter(prefix="/api", tags=["sidebar"], dependencies=[Depends(require_user)])


def _scope(c: dict) -> str:
    return c.get("project_slug") or f"chat-{c['id']}"


def _blocked_items(slug: str) -> int:
    """Plan items that stopped to ask the operator. A runner-made block (a
    dependency failed) is not a question, so it does not count."""
    try:
        p = plan.load(slug)
    except Exception:  # noqa: BLE001 — a corrupt plan file must not blank the sidebar
        return 0
    if not p:
        return 0
    return sum(1 for it in p.get("items", [])
               if it.get("status") == "blocked"
               and not (it.get("last_error") or "").startswith(plan.DEP_BLOCK))


@router.get("/sidebar")
async def sidebar():
    convos = (await chat.list_conversations())["conversations"]
    db = await get_db()
    try:
        folders = await chat._folder_rows(db)
        async with db.execute(
            "SELECT slug, name FROM projects WHERE deleted_at IS NULL AND is_hidden = 0 "
            "ORDER BY created_at DESC") as cur:
            projects = [dict(r) for r in await cur.fetchall()]
        # last activity per chat: the newest message, else when it started
        async with db.execute(
            "SELECT conversation_id AS id, MAX(created_at) AS at FROM messages "
            "GROUP BY conversation_id") as cur:
            last = {r["id"]: r["at"] for r in await cur.fetchall()}
        waiting: dict[str, dict] = {}

        def bump(slug: str, key: str, n: int) -> None:
            if n:
                waiting.setdefault(slug, {"git": 0, "egress": 0, "plan": 0})[key] += n

        async with db.execute(
            "SELECT project_slug AS slug, COUNT(*) AS n FROM git_requests "
            "WHERE status = 'pending' GROUP BY project_slug") as cur:
            for r in await cur.fetchall():
                bump(r["slug"], "git", r["n"])
        async with db.execute(
            "SELECT project_slug AS slug, COUNT(*) AS n FROM egress_pending "
            "WHERE status = 'pending' GROUP BY project_slug") as cur:
            for r in await cur.fetchall():
                bump(r["slug"], "egress", r["n"])
    finally:
        await db.close()
    for p in projects:
        bump(p["slug"], "plan", _blocked_items(p["slug"]))

    for c in convos:
        c["last_at"] = last.get(c["id"]) or c["started_at"]
    newest: dict[str, dict] = {}
    for c in convos:
        s = _scope(c)
        if s not in newest or c["last_at"] > newest[s]["last_at"]:
            newest[s] = c
    names = {p["slug"]: p["name"] for p in projects}
    needs = []
    for slug, counts in waiting.items():
        c = newest.get(slug)
        # an approval for a scope nobody can open (a deleted project, the
        # general policy row) has no home in the sidebar — Security shows it
        if c is None and slug not in names:
            continue
        needs.append({
            "scope": slug, "project": slug if slug in names else None,
            "conversation_id": c["id"] if c else None,
            "title": (c or {}).get("summary") or names.get(slug) or slug,
            "agent_slug": (c or {}).get("agent_slug"),
            "running": bool(c and c["running"]),
            **counts,
        })
    needs.sort(key=lambda n: (not n["running"], n["title"] or ""))
    return {
        "conversations": convos,
        "folders": folders,
        "projects": projects,
        "running": [c for c in convos if c["running"]],
        "needs": needs,
    }
