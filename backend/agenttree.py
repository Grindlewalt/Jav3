"""The agents tree, read for a person: what each node is, how it stands, and
what (if anything) it is waiting on the operator for.

GET /api/chat/agents (chat.py) returns the raw run tree — conversations nested
by parent. The operator's complaint was that it read like a database dump:
titles were whatever `conversations.summary` happened to hold ("[gen+mesh
perf] Project: /opt/jarvis/projects/benchmark-game ...", "[head] Plan: The
operator's request, verbatim:"), and the list was the fifty newest roots
whether or not anything in them still mattered. This module turns each node
into a title, a role, a status and a one-line `needs`, and splits the roots
into the ones that are active (running, or waiting on the operator) and the
ones that are finished, so a client can lead with the first.

Where "needs you" comes from — only waits that can be tied to a conversation:

* a plan item that is blocked or failed (the project's .plan.json). The plan
  cannot finish until the operator retries, edits or skips it. An item that
  never got a conversation (blocked on a dependency before it spawned) is put
  on the plan's head instead.
* a pending git commit/remote request (git_requests.conversation_id, recorded
  from the turn that filed it).
* a host in the egress approval queue that a node was denied recently
  (egress_events carries the conversation; the queue itself does not).
* an unacknowledged Review Center write flag (the diff gates' advisory trips;
  writes._raise_flag puts the conversation in the event detail).
* a /local write, edit or shell call the client is asking the operator about.

The last three are joined through a recent-activity window: the egress queue
and the review board both keep items for weeks, and a node that was denied a
host last month is not waiting on anything today.

Generated titles: a spawned agent's summary is its task cut at 40 characters,
mid-word. `name_later` asks the default model for a 3-6 word name once, when
the node is opened, and stores it in `conversations.title` (never `summary`,
which the peer roster and the runs views read as the task line)."""
from __future__ import annotations

import asyncio
import json
import re

from .db import get_db

TITLE_CHARS = 60
# how far back an egress denial or a review flag still counts as a node
# waiting on the operator (see the module docstring)
NEEDS_WINDOW_HOURS = 24
# scope=finished page size
FINISHED_DEFAULT = 100
FINISHED_MAX = 500
# the old default: how many of the newest roots scope=all shows, besides every
# running one
ALL_ROOTS = 50

# the naming pass is on in production; tests/conftest.py turns it off so a
# test that scripts model.complete does not see an extra call it never asked
# for (the naming test turns it back on)
NAMING = True
_naming_tasks: set[asyncio.Task] = set()

INTERRUPTED_PREFIX = "[Request interrupted"


# --- titles ---------------------------------------------------------------------

_TAG = re.compile(r"^\[([^\]]*)\]\s*")
_PREFIX = re.compile(r"^(?:plan|research)\s*:\s*", re.I)
# "Project: /opt/jarvis/projects/x" lead-in, and whatever separator follows it
_PROJECT = re.compile(r"^project\s*:\s*(\S+)\s*[-–—:;,.|]*\s*", re.I)
# a line that only introduces what follows. The plan runner's own dumps start
# with this one; any other line ending in ':' is treated the same way when
# more text follows it.
_LEADIN = re.compile(r"^(?:the\s+)?operator'?s\s+request,?\s*(?:verbatim)?\s*:?$", re.I)
_TRAIL = " \t:;,-–—|."


def _strip_leads(s: str) -> str:
    """Peel bracket tags, Plan:/Research: and a Project: lead-in off the front,
    in any order and any number of times ("[head] Plan: ..." is two)."""
    while True:
        before = s
        s = _TAG.sub("", s, count=1).lstrip()
        s = _PREFIX.sub("", s, count=1).lstrip()
        s = _PROJECT.sub("", s, count=1).lstrip()
        if s == before:
            return s


def _cut(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    head = s[:limit + 1]
    sp = head.rfind(" ")
    # a single enormous word (a URL) is cut where it must be rather than
    # leaving a stub
    head = head[:sp] if sp >= limit // 2 else s[:limit]
    return head.rstrip(_TRAIL) + "…"


def clean_title(text: str | None, limit: int = TITLE_CHARS) -> str:
    """A human title from whatever a node's text holds, or "" when nothing in
    it names anything (the caller then tries its next candidate)."""
    if not text:
        return ""
    lines = [ln.strip(" \t#>*-") for ln in str(text).splitlines()]
    lines = [ln for ln in lines if ln]
    # drop lead-in lines while something follows them: "Project: /x" alone,
    # "The operator's request, verbatim:", "Context:" ...
    while len(lines) > 1:
        first = _strip_leads(lines[0])
        if not first or _LEADIN.match(first) or first.endswith(":"):
            lines.pop(0)
            continue
        break
    s = _strip_leads(" ".join(" ".join(lines).split()))
    # a truncated summary ends in "..." and a lead-in in ':' — neither is part
    # of the title, and _cut adds its own ellipsis when it cuts
    s = s.rstrip(_TRAIL + "…").rstrip()
    if not s or _LEADIN.match(s):
        return ""
    return _cut(s, limit)


def _fallback(summary: str | None) -> str:
    """Last resort when every candidate cleaned to nothing ("[gen+mesh perf]
    Project: /opt/.../benchmark-game ..."): the tag and the project folder,
    which is at least what the operator typed."""
    s = " ".join((summary or "").split())
    tag = _TAG.match(s)
    parts = []
    if tag and tag.group(1).strip():
        parts.append(tag.group(1).strip())
        s = s[tag.end():]
    proj = _PROJECT.match(_PREFIX.sub("", s))
    if proj:
        parts.append(proj.group(1).rstrip("/.").rsplit("/", 1)[-1])
    return _cut(" · ".join(p for p in parts if p), TITLE_CHARS) or "untitled"


# --- plans ----------------------------------------------------------------------

_ITEM = re.compile(r"^\[item (i\d+)\]")


class Plans:
    """Every project's current .plan.json, indexed by the conversations it
    names: item.conversation_id -> the item, plan.root_id -> the plan."""

    def __init__(self) -> None:
        self.all: list[dict] = []
        self.items: dict[int, dict] = {}
        self.heads: dict[int, dict] = {}

    @classmethod
    async def load(cls, db) -> "Plans":
        from . import plan as plan_mod
        out = cls()
        async with db.execute("SELECT slug FROM projects") as cur:
            slugs = [r["slug"] for r in await cur.fetchall()]
        for slug in slugs:
            try:
                p = plan_mod.load(slug)
            except Exception:  # noqa: BLE001 — a broken plan file names nothing
                p = None
            if not p:
                continue
            out.all.append(p)
            if p.get("root_id"):
                out.heads[int(p["root_id"])] = p
            for it in p["items"]:
                if it.get("conversation_id"):
                    out.items[int(it["conversation_id"])] = it
        return out


def _plan_needs(plans: Plans, add) -> None:
    for p in plans.all:
        for it in p["items"]:
            if it["status"] not in ("blocked", "failed"):
                continue
            why = " ".join((it.get("last_error") or "no reason given").split())
            # an item's conversation may sit under an older head (an earlier
            # run of the same plan); it is still this plan's item and still waits
            if it.get("conversation_id"):
                add(it["conversation_id"], 1, f"plan item {it['status']}: {why}")
            elif p.get("root_id"):
                # never spawned (blocked on a dependency): the head holds it
                add(p["root_id"], 1, f"plan item {it['id']} {it['status']}: {why}")


# --- needs ----------------------------------------------------------------------

async def collect_needs(db, plans: Plans) -> dict[int, str]:
    """conversation id -> the one line saying why the operator is needed.
    Several reasons on one node read as the most pressing plus "(+N more)"."""
    found: dict[int, list[tuple[int, str]]] = {}

    def add(cid, prio: int, text: str) -> None:
        if cid is None:
            return
        found.setdefault(int(cid), []).append((prio, _cut(text, 140)))

    # /local: the client is asking the operator right now (priority 0: the
    # turn is parked on it)
    from . import localexec
    for (cid, _), p in list(localexec._pending.items()):
        if p.fut.done() or p.event.get("name") not in localexec.ASKING_TOOLS:
            continue
        args = p.event.get("args") or {}
        what = args.get("command") or args.get("path") or ""
        add(cid, 0, f"waiting on your answer: {p.event['name']}"
                    + (f" {' '.join(str(what).split())}" if what else ""))

    _plan_needs(plans, add)

    async with db.execute(
            "SELECT conversation_id, kind, message FROM git_requests "
            "WHERE status = 'pending' AND conversation_id IS NOT NULL") as cur:
        for r in await cur.fetchall():
            what = ("git commit: " + r["message"] if r["kind"] == "commit"
                    else "git remote " + r["message"])
            add(r["conversation_id"], 2, f"approval pending: {' '.join(what.split())}")

    window = f"-{NEEDS_WINDOW_HOURS} hours"
    async with db.execute(
            "SELECT DISTINCT e.conversation_id, p.host FROM egress_pending p "
            "JOIN egress_events e ON e.host = p.host AND e.project_slug = p.project_slug "
            "WHERE p.status = 'pending' AND e.conversation_id IS NOT NULL "
            "AND e.verdict IN ('deny', 'auto_deny') "
            "AND e.created_at >= datetime('now', ?) ORDER BY p.host", (window,)) as cur:
        for r in await cur.fetchall():
            add(r["conversation_id"], 3, f"approval pending: egress to {r['host']}")

    async with db.execute(
            "SELECT summary, detail FROM security_events WHERE acknowledged = 0 "
            "AND kind = 'write_flag' AND created_at >= datetime('now', ?)",
            (window,)) as cur:
        for r in await cur.fetchall():
            try:
                cid = (json.loads(r["detail"] or "{}") or {}).get("conversation_id")
            except (ValueError, AttributeError):
                cid = None
            if isinstance(cid, int):
                add(cid, 4, f"review pending: {r['summary']}")

    out = {}
    for cid, reasons in found.items():
        reasons.sort(key=lambda x: x[0])
        more = len(reasons) - 1
        out[cid] = reasons[0][1] + (f" (+{more} more)" if more else "")
    return out


# --- per-node reading -----------------------------------------------------------

def role_of(r: dict, plans: Plans) -> str:
    summary = r.get("summary") or ""
    if r.get("mode") == "orchestrate":
        return "orchestrator"
    if r["kind"] == "head":
        if r["id"] in plans.heads or summary.startswith("[head] Plan:"):
            return "plan"
        if summary.startswith("[head] Research:"):
            return "research"
        return "leader"          # a deploy_agents funnel head leads its team
    it = plans.items.get(r["id"])
    m = _ITEM.match(summary)
    if it is not None or m:
        return f"item {it['id'] if it is not None else m.group(1)}"
    if r.get("agent_slug"):
        return f"@{r['agent_slug']}"
    if r["kind"] in ("chat", "research", "scout", "reader", "leader", "subagent"):
        return r["kind"]
    return "agent"


def title_of(r: dict, plans: Plans) -> str:
    """The first candidate that cleans to something. A chat's summary is the
    chat naming pass's title, so it leads there; for agent work the full task
    (the first user message) beats the summary, which is that task cut at 40
    characters."""
    from . import plan as plan_mod
    cands: list[str | None] = [r.get("gen_title")]
    it = plans.items.get(r["id"])
    if it is not None:
        cands.append(it.get("title"))
    p = plans.heads.get(r["id"])
    if p is not None:
        cands.append(p.get("title"))
        if p.get("dump"):
            cands.append(plan_mod._title_from(p["dump"]))
    if r["kind"] == "chat":
        cands += [r.get("summary"), r.get("task")]
    else:
        cands += [r.get("task"), r.get("summary")]
    for c in cands:
        t = clean_title(c)
        if t:
            return t
    return _fallback(r.get("summary"))


def _ended_status(r: dict, plans: Plans) -> str:
    """How a node that is not running ended. Errors are not persisted by the
    turn paths (they go to the stream), so a transcript that ends on the
    operator's/task's message is a turn that died."""
    p = plans.heads.get(r["id"])
    if p is not None:
        return p["status"] if p["status"] in ("done", "failed", "stopped") else "stopped"
    it = plans.items.get(r["id"])
    if it is not None and it["status"] in ("done", "skipped"):
        return "done"
    rollup = r.get("rollup")
    if rollup is not None:
        return "failed" if rollup.startswith("error:") else "done"
    if r["kind"] == "head":
        return "stopped"         # no rollup and no budget: the job was lost
    if r.get("last_role") == "assistant":
        return "stopped" if (r.get("last_head") or "").startswith(INTERRUPTED_PREFIX) \
            else "done"
    return "failed"


def decorate(rows: list[dict], live: set[int], needs: dict[int, str],
             plans: Plans) -> None:
    """Add title/role/status/needs/ended_at to each row, in place. ended_at is
    the latest thing in the node's subtree (its own last message, its start,
    or a finished child's end), None while the node itself runs."""
    by_id = {r["id"]: r for r in rows}
    kids: dict[int, list[dict]] = {}
    for r in rows:
        if r["parent_id"] in by_id:
            kids.setdefault(r["parent_id"], []).append(r)
    done: set[int] = set()

    def visit(r: dict) -> str | None:
        if r["id"] in done:
            return r.get("ended_at")
        done.add(r["id"])
        ends = [e for e in (visit(k) for k in kids.get(r["id"], ())) if e]
        running = r["id"] in live
        r["needs"] = needs.get(r["id"])
        r["status"] = ("needs_you" if r["needs"] else "running" if running
                       else _ended_status(r, plans))
        r["ended_at"] = None if running else max(
            [e for e in (r.get("last_at"), r.get("started_at")) if e] + ends, default=None)
        return r["ended_at"]

    for r in rows:
        visit(r)
        r["role"] = role_of(r, plans)
        r["clean_title"] = title_of(r, plans)


# --- the naming pass ------------------------------------------------------------

async def _name(conversation_id: int, task: str) -> None:
    """chat._name_conversation for agent nodes: same default model, same
    prompt shape, one short call. Fails silently — the tree then cleans the
    task text instead."""
    from .agent.model import model
    try:
        final = None
        async for ev in model.complete([
            {"role": "system",
             "content": "Name this agent's task in 3-6 words. Reply with only the title."},
            {"role": "user", "content": task[:600]},
        ]):
            if ev["type"] == "message":
                final = ev
        # models like to quote a title; chat's pass strips the same way
        title = clean_title(((final or {}).get("content") or "").strip().strip("\"'`"))
        if not title:
            return
        db = await get_db()
        try:
            await db.execute("UPDATE conversations SET title = ? WHERE id = ?",
                             (title, conversation_id))
            await db.commit()
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — a name is a nicety, never a failure
        pass


def name_later(conversation_id: int, task: str) -> None:
    """Start the naming pass in the background. It inherits the caller's
    context, so inside a job the call counts against that job's Budget like
    every other call the job makes."""
    if not NAMING or not (task or "").strip():
        return
    try:
        t = asyncio.get_running_loop().create_task(_name(conversation_id, task))
    except RuntimeError:
        return
    _naming_tasks.add(t)                 # a bare task can be collected mid-flight
    t.add_done_callback(_naming_tasks.discard)
