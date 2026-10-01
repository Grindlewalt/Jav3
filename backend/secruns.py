"""The Security Queue, one card per RUN (SB2, 2026-10-01).

An event is rarely interesting alone: 47 "scratch file deleted" lines and two
"new program" alerts from one agent run are one story. This module groups the
log by the run an event belongs to, and says what the agent was doing at the
step it happened in.

Group key, first that applies (the rules, in order):

  run:<root>             the event names a conversation (its column, or the
                         `conversation_id` the older raise sites put in the
                         detail); <root> is the top of that conversation's tree:
                         the chat, or the plan / funnel job head. A subagent's
                         event files under the chat that spawned it.
  box:<id>[:<boot>]      no conversation, but a box (process and proxy events
                         nobody's turn was bound to): the box and its boot, so a
                         rebooted box's alerts start a new card.
  proj:<project>:<kind>  neither: the project and the kind (a login burst, a
                         backup change). No project: kind:<kind>.

A card holds what waits on the operator ("need you": critical, approval and
alert tier), the agent's own reports (harness_fault, info), and how many rows
rules filed as normal work (quiet='rule', already acknowledged: shown on
request). Record-tier rows that wait are counted, never listed. A card with
only info/record rows waiting resolves itself once its run has ended (nothing
live in the tree, nothing done for SETTLE_S): see sweep_finished.

"What the agent was doing" (`doing`): the tool call the event happened in (by
the model's call id; else the call whose command the process came from; else
the nearest earlier call), its command, and what the agent last said it was
doing (the text it wrote before that call, its todo item, the chat's title).
All of that is the agent's own text: UNTRUSTED. It is clipped here, handed over
as plain strings with `untrusted: true`, and every client renders it as text.
"""
import json
import re
from pathlib import PurePosixPath

import aiosqlite

from . import security

TIER_RANK = {"critical": 3, "approval": 2, "alert": 1, "record": 0}
SEV_RANK = {"info": 0, "warn": 1}          # a severity not listed (the top one) ranks above both
SETTLE_S = 300            # a run with nothing live and nothing done this long has ended
FILTERED_DAYS = 14        # the "filtered as normal work" count looks this far back
SCAN_WAITING = 3000       # rows read per request: bounded, newest first
SCAN_FILTERED = 6000
MAX_IDS = 60              # event ids a card kind line carries
MAX_SUBJECTS = 6

# clipping of agent-written text in `doing`
CLIP_COMMAND = 400
CLIP_ARGS = 400
CLIP_SAYS = 600
_CTRL = re.compile("[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f\\u2028\\u2029\\u202a-\\u202e\\u2066-\\u2069]")


def _clip(s, n: int) -> str:
    s = _CTRL.sub(" ", str(s if s is not None else ""))
    s = s.strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _detail(ev: dict) -> dict:
    d = ev.get("detail")
    return d if isinstance(d, dict) else {}


def _int(v) -> int | None:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def event_conversation(ev: dict) -> int | None:
    """The conversation an event names: its own column, else the detail the older
    raise sites wrote (write_flag, harness_fault, memory, journal, package)."""
    return _int(ev.get("conversation_id")) or _int(_detail(ev).get("conversation_id"))


def event_box(ev: dict) -> tuple[str | None, str | None]:
    d = _detail(ev)
    box = ev.get("box_id") or d.get("box_id") or d.get("box")
    boot = ev.get("boot_id") or d.get("boot_id")
    return (str(box) if box else None), (str(boot) if boot else None)


def subject_of(ev: dict) -> str:
    """The one thing an event is about, short: the file, the host, the program."""
    d = _detail(ev)
    for k in ("path", "host", "username", "peer"):
        if d.get(k):
            return _clip(d[k], 120)
    exe = d.get("exe") or d.get("path")
    if exe:
        return _clip(PurePosixPath(str(exe)).name or exe, 80)
    return _clip(d.get("unit") or d.get("name") or "", 80)


async def _root(db, ev: dict, cache: dict) -> int | None:
    cid = event_conversation(ev)
    if cid is None:
        return None
    root = _int(ev.get("run_root"))
    if root:
        return root
    if cid not in cache:
        cache[cid] = await security.run_root_of(db, cid)
    return cache[cid]


def key_for(ev: dict, root: int | None) -> tuple[str, str]:
    """(group key, 'run' | 'box' | 'proj') — the rules in the module doc."""
    if root:
        return f"run:{root}", "run"
    box, boot = event_box(ev)
    if box:
        return (f"box:{box}:{boot[:8]}" if boot else f"box:{box}"), "box"
    proj = ev.get("project_slug") or ev.get("project")
    if proj:
        return f"proj:{proj}:{ev.get('kind')}", "proj"
    return f"kind:{ev.get('kind')}", "proj"


async def _conv(db, cid: int, cache: dict) -> dict | None:
    if cid not in cache:
        async with db.execute(
                "SELECT c.id, c.kind, c.summary, c.agent_slug, c.job_id, "
                "c.parent_conversation_id AS parent, p.slug AS project "
                "FROM conversations c LEFT JOIN projects p ON p.id = c.project_id "
                "WHERE c.id = ?", (cid,)) as cur:
            r = await cur.fetchone()
        cache[cid] = dict(r) if r else None
    return cache[cid]


def _root_label(c: dict | None, root: int) -> str:
    if c is None:
        return f"chat {root} (deleted)"
    if c.get("job_id"):
        what, ident = "job", str(c["job_id"])[:8]
    else:
        what, ident = ((c.get("kind") or "chat"), str(root))
    label = f"{what} {ident}"
    if c.get("summary"):
        label += f' "{_clip(c["summary"], 48)}"'
    return label


async def _running_roots(db) -> set[int] | None:
    """The run roots with a loop in flight right now, or None when that cannot
    be known (it then reads as 'still running': never end a run on a guess)."""
    try:
        from . import chat
        ids = chat._running_loops()
    except Exception:                               # noqa: BLE001
        return None
    roots: set[int] = set()
    for i in ids:
        roots.add(await security.run_root_of(db, i) or i)
    return roots


async def _settled(db, root: int) -> bool:
    """Has nothing happened in this run's tree for SETTLE_S? A deleted run counts."""
    async with db.execute(
            "WITH RECURSIVE tree(id) AS (SELECT id FROM conversations WHERE id = ? "
            "UNION SELECT c.id FROM conversations c JOIN tree t "
            "ON c.parent_conversation_id = t.id) "
            "SELECT (SELECT COUNT(*) FROM tree) AS n, "
            "(SELECT MAX(created_at) FROM tool_calls WHERE conversation_id IN tree) AS t, "
            "(SELECT MAX(created_at) FROM messages WHERE conversation_id IN tree) AS m, "
            "(SELECT started_at FROM conversations WHERE id = ?) AS s, "
            "datetime('now', ?) AS cutoff", (root, root, f"-{SETTLE_S} seconds")) as cur:
        r = await cur.fetchone()
    if not r["n"]:
        return True
    last = max((x for x in (r["t"], r["m"], r["s"]) if x), default=None)
    return last is None or last <= r["cutoff"]


async def _scan(db) -> list[dict]:
    """The rows a Queue needs: everything waiting, plus the rows rules filed as
    normal work in the last FILTERED_DAYS (counted, not listed)."""
    cols = security._COLUMNS
    out: list[dict] = []
    async with db.execute(f"SELECT {cols} FROM security_events WHERE acknowledged = 0 "
                          "ORDER BY id DESC LIMIT ?", (SCAN_WAITING,)) as cur:
        out += [security._row(r) for r in await cur.fetchall()]
    async with db.execute(
            f"SELECT {cols} FROM security_events WHERE acknowledged = 1 AND quiet = 'rule' "
            "AND created_at >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
            (f"-{FILTERED_DAYS} days", SCAN_FILTERED)) as cur:
        out += [security._row(r) for r in await cur.fetchall()]
    return out


def _bucket(ev: dict, level: str, prefs: dict) -> str:
    """need | report | record | filtered: where a row sits in its card."""
    if ev.get("acknowledged"):
        return "filtered" if ev.get("quiet") == "rule" else "done"
    if ev["kind"] in security.QUEUE_KEEPS:
        return "report"
    t = security.effective_tier(ev["kind"], ev["severity"], level, prefs)
    ev["tier"] = t
    return "record" if t == "record" else "need"


def _worst(a: str | None, b: str | None) -> str | None:
    return b if a is None or TIER_RANK.get(b or "", -1) > TIER_RANK.get(a or "", -1) else a


async def _groups(db) -> tuple[dict, dict]:
    """(key -> group, {conversation id: conversation row}) for every row in scope."""
    level, prefs = await security.notify_level(db), await security.get_prefs(db)
    roots_cache: dict = {}
    convs: dict = {}
    groups: dict[str, dict] = {}
    for ev in await _scan(db):
        bucket = _bucket(ev, level, prefs)
        if bucket == "done":
            continue
        root = await _root(db, ev, roots_cache)
        key, gkind = key_for(ev, root)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"key": key, "group": gkind, "root": root, "project": None,
                               "need": [], "report": [], "record": [], "filtered": []}
        g[bucket].append(ev)
        g["project"] = g["project"] or ev.get("project_slug")
    for g in groups.values():
        if g["root"]:
            g["conv"] = await _conv(db, g["root"], convs)
            if g["conv"] and g["conv"].get("project"):
                g["project"] = g["conv"]["project"]
        else:
            g["conv"] = None
    return groups, convs


def _kind_lines(rows: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for ev in rows:                                   # newest first
        k = by.setdefault(ev["kind"], {"kind": ev["kind"], "n": 0, "count": 0,
                                       "severity": ev["severity"], "tier": ev.get("tier"),
                                       "subjects": [], "ids": []})
        k["n"] += 1
        k["count"] += ev.get("count") or 1
        if TIER_RANK.get(ev.get("tier"), 0) > TIER_RANK.get(k["tier"], 0):
            k["tier"], k["severity"] = ev.get("tier"), ev["severity"]
        s = subject_of(ev)
        if s and s not in k["subjects"] and len(k["subjects"]) < MAX_SUBJECTS:
            k["subjects"].append(s)
        if len(k["ids"]) < MAX_IDS:
            k["ids"].append(ev["id"])
    return sorted(by.values(), key=lambda k: (-TIER_RANK.get(k["tier"], 0), -k["ids"][0]))


def _title(g: dict) -> str:
    if g["group"] == "run":
        head = _root_label(g["conv"], g["root"])
        return f"{g['project']} · {head}" if g["project"] else head
    parts = g["key"].split(":")
    if g["group"] == "box":
        return f"box {parts[1]}" + (f" · boot {parts[2]}" if len(parts) > 2 else "")
    return (f"{g['project']} · {parts[2]}" if len(parts) > 2 and g["project"]
            else parts[-1])


def _card(g: dict, running) -> dict:
    rows = g["need"] + g["report"]
    tiers = [ev.get("tier") for ev in g["need"]]
    worst = None
    for t in tiers:
        worst = _worst(worst, t)
    if worst is None and g["report"]:
        worst = "record"
    sevs = [ev["severity"] for ev in g["need"] if ev.get("tier") == worst]
    sevs = sevs or [ev["severity"] for ev in g["report"]]
    sev = max(sevs, key=lambda s: SEV_RANK.get(s, 2), default=None)   # the worst in the card
    newest = max((ev["id"] for ev in rows), default=0)
    stamps = [ev.get("last_seen") or ev.get("created_at") or "" for ev in rows]
    c = g["conv"]
    return {
        "key": g["key"], "group": g["group"], "title": _title(g), "project": g["project"],
        "root": ({"id": g["root"], "kind": (c or {}).get("kind"),
                  "summary": _clip((c or {}).get("summary"), 80),
                  "agent_slug": (c or {}).get("agent_slug"),
                  "job_id": (c or {}).get("job_id")} if g["root"] else None),
        "running": running, "tier": worst, "severity": sev,
        "counts": {"need": len(g["need"]), "reports": len(g["report"]),
                   "filtered": len(g["filtered"]), "record": len(g["record"])},
        "kinds": _kind_lines(g["need"]),
        "report_kinds": _kind_lines(g["report"]),
        "newest_id": newest, "last_at": max(stamps, default=""),
    }


async def list_runs(db: aiosqlite.Connection, *, queue: bool = True, sweep: bool = True) -> dict:
    """The Queue's cards. `queue` keeps the groups with something visible waiting
    (need-you or an agent report); `sweep` first resolves the info-only groups
    whose run has ended."""
    if sweep:
        await sweep_finished(db)
    groups, _ = await _groups(db)
    running = await _running_roots(db)
    cards = []
    for g in groups.values():
        if queue and not (g["need"] or g["report"]):
            continue
        if not queue and not (g["need"] or g["report"] or g["record"] or g["filtered"]):
            continue
        run = None if g["group"] != "run" or running is None else g["root"] in running
        cards.append(_card(g, run))
    cards.sort(key=lambda c: (-TIER_RANK.get(c["tier"], -1), -c["newest_id"]))
    return {"runs": cards,
            "totals": {"runs": len(cards), "need": sum(c["counts"]["need"] for c in cards),
                       "reports": sum(c["counts"]["reports"] for c in cards)}}


async def sweep_finished(db: aiosqlite.Connection) -> int:
    """Resolve the groups that hold only info/record rows, once their run has
    ended. Returns the rows acknowledged. Unknown liveness resolves nothing."""
    groups, _ = await _groups(db)
    cand = [g for g in groups.values()
            if g["group"] == "run" and not g["need"] and (g["report"] or g["record"])]
    if not cand:
        return 0
    running = await _running_roots(db)
    if running is None:
        return 0
    n = 0
    for g in cand:
        if g["root"] in running or not await _settled(db, g["root"]):
            continue
        for ev in g["report"] + g["record"]:
            await security.acknowledge(db, ev["id"])
            n += 1
    return n


# --- one card, with the evidence ----------------------------------------------

async def _call_row(db, ev: dict, cid: int):
    """The tool_calls row an event happened in: (row, how). By the model's call
    id; else, for a process, the latest earlier call whose arguments name it;
    else the nearest earlier call."""
    cols = "id, tool, args, result, created_at, call_id"
    call = ev.get("call_id") or _detail(ev).get("call_id")
    if call:
        async with db.execute(f"SELECT {cols} FROM tool_calls WHERE conversation_id = ? "
                              "AND call_id = ? ORDER BY id DESC LIMIT 1", (cid, str(call))) as cur:
            r = await cur.fetchone()
        if r:
            return dict(r), "call"
    at = ev.get("created_at") or ""
    d = _detail(ev)
    cmd = d.get("cmd") or ""
    if ev["kind"] in ("unexpected_process", "proc_report_mismatch") and cmd:
        toks = [t for t in re.split(r"\s+", str(cmd))[1:] if len(t) >= 4 and not t.startswith("-")]
        toks = [PurePosixPath(t).name or t for t in toks][:4]
        if toks:
            async with db.execute(
                    f"SELECT {cols} FROM tool_calls WHERE conversation_id = ? AND created_at <= ? "
                    "ORDER BY id DESC LIMIT 80", (cid, at)) as cur:
                for r in await cur.fetchall():
                    if any(t in (r["args"] or "") for t in toks):
                        return dict(r), "command"
    async with db.execute(f"SELECT {cols} FROM tool_calls WHERE conversation_id = ? "
                          "AND created_at <= ? ORDER BY id DESC LIMIT 1", (cid, at)) as cur:
        r = await cur.fetchone()
    return (dict(r), "nearest") if r else (None, None)


def _command_of(tool: str, args: dict) -> str:
    for k in ("command", "cmd", "code", "script", "url", "path", "query", "text"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return _clip(v, CLIP_COMMAND)
    return ""


async def _says(db, cid: int, step: dict | None, conv: dict | None) -> dict | None:
    """What the agent last said it was doing, as text: the narration before the
    step, else the latest todo it wrote, else the chat's title."""
    if step is not None:
        async with db.execute(
                "SELECT n.text, (SELECT COUNT(*) FROM tool_calls t WHERE t.conversation_id = ? "
                "AND t.id > n.after_call_id AND t.id < ?) AS gap "
                "FROM turn_narration n WHERE n.conversation_id = ? AND n.after_call_id < ? "
                "ORDER BY n.id DESC LIMIT 1", (cid, step["id"], cid, step["id"])) as cur:
            r = await cur.fetchone()
        if r and r["text"] and (r["gap"] or 0) <= 12:
            return {"text": _clip(r["text"], CLIP_SAYS), "source": "narration"}
        async with db.execute(
                "SELECT args FROM tool_calls WHERE conversation_id = ? AND tool = 'todo_update' "
                "AND id < ? ORDER BY id DESC LIMIT 20", (cid, step["id"])) as cur:
            for t in await cur.fetchall():
                try:
                    a = json.loads(t["args"] or "{}")
                except ValueError:
                    continue
                if a.get("action") == "add":
                    items = a.get("items") if isinstance(a.get("items"), list) else [a.get("text")]
                    text = " / ".join(str(i) for i in items if i)
                    if text:
                        return {"text": _clip(text, CLIP_SAYS), "source": "todo"}
    if conv and conv.get("summary"):
        return {"text": _clip(conv["summary"], CLIP_SAYS), "source": "chat title"}
    return None


async def doing(db: aiosqlite.Connection, ev: dict, convs: dict | None = None) -> dict | None:
    """What the agent was doing when this event happened (see the module doc),
    or None for an event with no conversation."""
    cid = event_conversation(ev)
    if cid is None:
        return None
    conv = await _conv(db, cid, convs if convs is not None else {})
    row, how = await _call_row(db, ev, cid)
    step = None
    if row is not None:
        try:
            args = json.loads(row["args"] or "{}")
        except ValueError:
            args = {}
        args = args if isinstance(args, dict) else {}
        step = {"id": row["id"], "call_id": row["call_id"], "tool": row["tool"],
                "at": row["created_at"], "match": how,
                "command": _command_of(row["tool"], args),
                "args": _clip(json.dumps(args, ensure_ascii=False), CLIP_ARGS)}
    return {"conversation": ({"id": cid, "kind": conv.get("kind"), "agent_slug": conv.get("agent_slug"),
                              "summary": _clip(conv.get("summary"), 80)} if conv
                             else {"id": cid, "deleted": True}),
            "step": step, "says": await _says(db, cid, row, conv), "untrusted": True}


async def run_detail(db: aiosqlite.Connection, key: str) -> dict | None:
    """One card with its events (need-you and reports, newest first, each with
    `doing`) and the filtered rows' one-liners. None when no such group."""
    groups, convs = await _groups(db)
    g = groups.get(key)
    if g is None:
        return None
    running = await _running_roots(db)
    run = None if g["group"] != "run" or running is None else g["root"] in running
    card = _card(g, run)
    events = []
    for ev in sorted(g["need"] + g["report"], key=lambda e: -e["id"]):
        events.append({**ev, "doing": await doing(db, ev, convs)})
    filtered = [{"id": e["id"], "kind": e["kind"], "summary": e["summary"], "rule": e.get("rule"),
                 "created_at": e["created_at"], "count": e.get("count") or 1,
                 "subject": subject_of(e)} for e in g["filtered"][:200]]
    return {**card, "events": events, "filtered": filtered,
            "records": [e["id"] for e in g["record"]][:MAX_IDS]}


async def acknowledge_group(db: aiosqlite.Connection, key: str, *,
                            only: str | None = None) -> dict:
    """Acknowledge what waits in one card: `only` = 'reports' (the agent reports),
    'alerts' (everything else), None = all of it, record rows included."""
    groups, _ = await _groups(db)
    g = groups.get(key)
    if g is None:
        return {"ok": True, "done": 0}
    rows = ((g["report"] if only in (None, "reports") else [])
            + (g["need"] + g["record"] if only in (None, "alerts") else []))
    for ev in rows:
        await security.acknowledge(db, ev["id"])
    return {"ok": True, "done": len(rows)}
