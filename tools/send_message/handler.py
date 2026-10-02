import json

from backend import agentmsg, runtime
from backend.db import get_db

# one name for "every unfinished item of the plan I am in" (item:* is the
# spelling a model reaches for after seeing item:<id> addresses)
BROADCAST = ("items", "item:*")
# a plan holds at most 24 items (orchestrator.MAX_NODES); a list may not outgrow it
MAX_RECIPIENTS = 24


def _addresses(to) -> list[str]:
    """The addresses asked for: a list, or one string (comma-separated is
    tolerated — no slug, id or item address contains a comma)."""
    if isinstance(to, str) and to.lstrip().startswith("["):
        try:                              # a list the model sent as JSON text
            to = json.loads(to)
        except ValueError:
            pass
    parts = to if isinstance(to, (list, tuple)) else str(to or "").split(",")
    return [str(p).strip() for p in parts if str(p).strip()]


async def _plan_items(cid: int) -> tuple[list[str], str | None]:
    """(item:<id> addresses, why none): the plan of the sender's project, its
    items that are running (delivered live) or still to start (kept as a note
    in the brief they will be run with). Done, failed, blocked and skipped
    items cannot read it. The sender's own item is left out."""
    from backend import plan as plan_mod
    mine = plan_mod.live_item(cid)
    project = mine["project"] if mine else None
    if project is None:
        db = await get_db()
        try:
            async with db.execute(
                "SELECT p.slug FROM conversations c "
                "LEFT JOIN projects p ON p.id = c.project_id WHERE c.id = ?", (cid,)) as cur:
                row = await cur.fetchone()
        finally:
            await db.close()
        project = row["slug"] if row else None
    if not project:
        return [], "you are not in a project, so there is no plan to broadcast to"
    plan = plan_mod.load(project)
    if plan is None:
        return [], f"project {project} has no plan"
    own = mine["item_id"] if mine else None
    ids = [f"item:{it['id']}" for it in plan["items"]
           if it["status"] in ("running", "todo") and it["id"] != own]
    if not ids:
        return [], "no other item of the plan is running or waiting to start"
    return ids, None


def _gist(text: str) -> str:
    """What happened to one send, from agentmsg.send_tool's sentence. An unknown
    sentence is kept whole, so a reworded one is never misreported."""
    if text.startswith("sent to"):
        return "running now, reads it on its next round"
    if text.startswith("queued for"):
        return "not running, reads it when it next starts"
    if text.startswith("kept as a note"):
        return "not started, reads it in its brief"
    return text


async def run(to: str | list[str], message: str) -> str:
    # Deliberately no `from`/sender argument: identity is resolved host-side
    # from the turn envelope (agentmsg.send_tool), so a compromised guest has
    # nothing to forge.
    addrs = _addresses(to)
    if len(addrs) <= 1 and (not addrs or addrs[0].lower() not in BROADCAST):
        # the single-recipient call, unchanged ("?" lookups, empty message, ...)
        return await agentmsg.send_tool(addrs[0] if addrs else "", message)
    if any(a.lower() in ("?", "list", "who") for a in addrs):
        return await agentmsg.send_tool("?", message)         # a lookup sends nothing
    if not (message or "").strip():
        return "error: a message to several recipients needs the message text."
    cid = runtime.conversation_id.get()
    targets, failed = [], []              # failed: (address, why)
    for a in addrs:
        if a.lower() in BROADCAST:
            ids, why = await _plan_items(cid) if cid else ([], "this call has no turn identity")
            if why:
                failed.append((a, why))
            targets += ids
        else:
            targets.append(a)
    seen, unique = set(), []
    for a in targets:                     # twice named is one message
        k = a.lstrip("#@").strip().lower()
        if k not in seen:
            seen.add(k)
            unique.append(a)
    if len(unique) > MAX_RECIPIENTS:
        return (f"error: {len(unique)} recipients is over the limit of {MAX_RECIPIENTS}. "
                "Nothing was sent.")
    sent: dict[str, list[str]] = {}       # gist -> addresses
    for a in unique:
        out = await agentmsg.send_tool(a, message)
        if out.startswith("error:"):
            failed.append((a, out[len("error:"):].strip()))
        else:
            sent.setdefault(_gist(out.replace(" Do not wait for a reply this turn.", "")),
                            []).append(a)
    if not sent and len({why for _, why in failed}) == 1:
        return "error: " + failed[0][1]   # the same refusal for everyone (incognito, no turn)
    n = sum(len(v) for v in sent.values())
    lines = [f"Sent to {n} of {n + len(failed)} (one message each):"] if sent else \
            ["Nothing was sent:"]
    for gist, who in sent.items():
        lines.append(f"  {', '.join(who)} — {gist}")
    if sent and failed:
        lines.append("Not sent:")
    for a, why in failed:
        lines.append(f"  {a} — {why[:160]}")
    if sent:
        lines.append("Do not wait for replies this turn.")
    return "\n".join(lines)
