"""Per-project egress policy — the fine-grained half of Layer 3.

nftables gives the coarse floor (drop LAN, force DNS through the host resolver,
redirect 80/443 to the host proxy, drop everything else). THIS module is what
the proxy consults per request to decide allow / deny / cut on the *hostname*,
and it owns the approval queue that trains the allowlist up.

The model the operator chose: a project with no policy row inherits the shared
`__general__` baseline allowlist (seeded from settings.egress_seed_hosts —
deny-by-default vs the open internet — which trains up as hosts are approved).
A *sensitive* project gets its own row: a scoped allowlist (inherit_general=0),
an allow-by-default denylist (mode='denylist'), or full deny (mode='denyall',
i.e. netless-equivalent for that project).

A new/unapproved host is DENIED and queued — that is routine, not an alarm.
Only exfil-shaped behaviour (backend/anomaly.py) raises a security_event and a
`cut`, which this module records so the proxy refuses the host immediately.
"""
import json

import aiosqlite

from . import bus
from .config import settings

GENERAL = "__general__"          # the shared baseline policy row's slug
EGRESS_CHAN = "egress"           # bus channel the live Network view subscribes to

# The proxy sees raw guest requests with no op_id, so egress is attributed to the
# operation currently driving the single guest. The broker sets this on
# register_turn (innermost/most-recent wins; nested turns share the project). A
# plain module global — not a contextvar — because the proxy runs on a different
# asyncio task than the turn.
#
# It is a STACK because turns overlap in both directions: a spawn_agent child
# registers while its parent is still open, and several chats can drive the one
# guest at once. So a turn ending has to hand attribution back to whatever is
# still running rather than blanking it — and, when nothing is, actually clear.
# Until 2026-08-10 it never cleared at all: `release_turn` dropped the envelope
# and left the finished project's slug in place, so anything the guest did
# afterwards (a process outliving its run_code call, a straggling connection)
# was policed under the last project to have run. Unattributed traffic now
# falls back to the general baseline, which is what the very first request
# after boot has always used.
_EMPTY: dict = {"project": None, "op_id": None, "conversation_id": None}
_context: dict = dict(_EMPTY)
_stack: list[dict] = []

# A safety valve, not a design limit: every push is paired with a pop in
# guest_turn's `finally`, so real depth is the number of turns in flight (a
# handful). If that pairing is ever broken, drop the oldest rather than grow a
# list forever in a process that runs for weeks.
_STACK_CAP = 64


def set_context(project: str | None, op_id: str | None = None,
                conversation_id: int | None = None) -> None:
    """Attribute the guest's egress to this turn, until it releases."""
    entry = {"project": project, "op_id": op_id, "conversation_id": conversation_id}
    _stack.append(entry)
    del _stack[:-_STACK_CAP]
    _context.update(entry)


def clear_context(op_id: str | None) -> None:
    """Drop a finished turn's attribution, restoring the turn underneath it.

    Removes THIS op's entry wherever it sits, not the top one: with concurrent
    turns the one that finishes first is usually not the one that started last."""
    for i in range(len(_stack) - 1, -1, -1):
        if _stack[i]["op_id"] == op_id:
            del _stack[i]
            break
    _context.update(_stack[-1] if _stack else _EMPTY)


def current_context() -> dict:
    return dict(_context)

# (project_slug, host) pairs auto-cut this process. The nft drop (Pi-side) is
# the hard block; this in-memory set is what the proxy checks synchronously so a
# cut takes effect on the very next request without a DB round-trip.
_cut: set[tuple[str, str]] = set()


def _host_matches(host: str, patterns: list[str]) -> bool:
    """Exact or subdomain match, same rule as secrets._host_allowed."""
    host = (host or "").lower().rstrip(".")
    for p in patterns:
        p = (p or "").lower().strip()
        if p and (host == p or host.endswith("." + p)):
            return True
    return False


async def _row(db: aiosqlite.Connection, slug: str) -> dict | None:
    async with db.execute(
            "SELECT project_slug, mode, inherit_general, hosts FROM egress_policy "
            "WHERE project_slug = ?", (slug,)) as cur:
        r = await cur.fetchone()
    return dict(r) if r else None


async def ensure_general(db: aiosqlite.Connection) -> None:
    """Seed the shared baseline row from the config seed list, once."""
    if await _row(db, GENERAL) is None:
        await db.execute(
            "INSERT OR IGNORE INTO egress_policy(project_slug, mode, inherit_general, hosts) "
            "VALUES (?, 'allowlist', 0, ?)",
            (GENERAL, json.dumps(sorted(set(settings.egress_seed_hosts)))))
        await db.commit()


async def get_policy(db: aiosqlite.Connection, slug: str) -> dict:
    """Effective policy for a project: its own row if it has one, else the
    general baseline. Returns {slug, mode, inherit_general, hosts, effective,
    source} where `effective` is the resolved allow/deny host list the proxy
    uses and `source` is 'project' or 'general'."""
    await ensure_general(db)
    general = await _row(db, GENERAL) or {"hosts": "[]"}
    gen_hosts = json.loads(general["hosts"] or "[]")
    own = await _row(db, slug) if slug and slug != GENERAL else None
    if own is None:
        return {"slug": slug, "mode": "allowlist", "inherit_general": 1,
                "hosts": [], "effective": gen_hosts, "source": "general"}
    hosts = json.loads(own["hosts"] or "[]")
    effective = hosts + gen_hosts if (own["inherit_general"] and own["mode"] == "allowlist") else hosts
    return {"slug": slug, "mode": own["mode"], "inherit_general": own["inherit_general"],
            "hosts": hosts, "effective": effective, "source": "project"}


# The one deny reason egress auto mode may act on: an allowlist-mode project
# meeting a host nobody has decided about. Every other deny (denyall, denylist,
# cut) is a standing decision the guesser must never second-guess.
NOT_LISTED = "host not on the allowlist (queued for approval)"


async def decide(db: aiosqlite.Connection, slug: str, host: str) -> tuple[str, str]:
    """(verdict, reason) for one host. verdict ∈ {allow, deny, cut}."""
    if (slug, host) in _cut or (GENERAL, host) in _cut:
        return "cut", "host auto-cut after an anomaly"
    pol = await get_policy(db, slug)
    if pol["mode"] == "denyall":
        return "deny", "egress disabled for this project"
    if pol["mode"] == "denylist":
        if _host_matches(host, pol["hosts"]):
            return "deny", "host on the project denylist"
        return "allow", "allow-by-default (denylist mode)"
    # allowlist (deny-by-default)
    if _host_matches(host, pol["effective"]):
        return "allow", f"host on the {pol['source']} allowlist"
    auto = await active_auto(db, slug, host)
    if auto:
        return "allow", f"auto-allowed until {auto['expires_at']} UTC: {auto['reason']}"
    return "deny", NOT_LISTED


# --- auto-allowed hosts (egress auto mode) -------------------------------------
# Deliberately NOT part of get_policy()['effective']: the triage reviewer
# approves anything already "on the effective allowlist" onto the real list,
# which would silently promote a guess into a permanent entry. An auto entry is
# exact-host (no subdomains), scoped to exactly one slug, and time-boxed.

_AUTO_LIVE = ("revoked_at IS NULL AND promoted_at IS NULL "
              "AND expires_at > datetime('now')")


async def active_auto(db: aiosqlite.Connection, slug: str, host: str) -> dict | None:
    async with db.execute(
            f"SELECT id, host, rule, reason, created_at, expires_at FROM egress_auto_allow "
            f"WHERE project_slug = ? AND host = ? AND {_AUTO_LIVE} "
            f"ORDER BY id DESC LIMIT 1", (slug, (host or "").lower())) as cur:
        r = await cur.fetchone()
    return dict(r) if r else None


async def auto_allows_today(db: aiosqlite.Connection, slug: str) -> int:
    """Auto-allows granted to this project in the last 24h — revoked or not,
    so revoking does not refill the cap."""
    async with db.execute(
            "SELECT COUNT(*) AS n FROM egress_auto_allow WHERE project_slug = ? "
            "AND created_at > datetime('now', '-1 day')", (slug,)) as cur:
        return (await cur.fetchone())["n"]


async def add_auto(db: aiosqlite.Connection, slug: str, host: str, *, rule: str,
                   reason: str) -> dict:
    days = max(1, int(settings.egress_auto_ttl_days))
    cur = await db.execute(
        "INSERT INTO egress_auto_allow(project_slug, host, rule, reason, expires_at) "
        "VALUES (?, ?, ?, ?, datetime('now', ?))",
        (slug, host.lower(), rule, reason, f"+{days} days"))
    await db.commit()
    async with db.execute("SELECT expires_at FROM egress_auto_allow WHERE id = ?",
                          (cur.lastrowid,)) as c:
        exp = (await c.fetchone())["expires_at"]
    return {"id": cur.lastrowid, "expires_at": exp}


async def revoke_auto(db: aiosqlite.Connection, auto_id: int) -> dict:
    """Operator revokes a guess. The queue row is marked so auto mode never
    re-grants the same host to the same project on its next retry — once the
    operator has said no, only the operator can say yes."""
    async with db.execute("SELECT project_slug, host FROM egress_auto_allow WHERE id = ?",
                          (auto_id,)) as cur:
        r = await cur.fetchone()
    if r is None:
        return {"ok": False, "error": "no such auto-allowed host"}
    await db.execute("UPDATE egress_auto_allow SET revoked_at = datetime('now') "
                     "WHERE id = ? AND revoked_at IS NULL", (auto_id,))
    await db.execute(
        "UPDATE egress_pending SET status = 'rejected', decided_at = datetime('now'), "
        "auto_verdict = 'revoked', auto_reason = 'operator revoked the auto-allow', "
        "auto_at = datetime('now') WHERE project_slug = ? AND host = ?",
        (r["project_slug"], r["host"]))
    await db.commit()
    return {"ok": True, "project": r["project_slug"], "host": r["host"]}


async def promote_auto(db: aiosqlite.Connection, auto_id: int) -> dict:
    """Operator keeps a guess: it moves onto the real allowlist (same training
    rule as an approval) and stops expiring."""
    async with db.execute(
            f"SELECT project_slug, host FROM egress_auto_allow WHERE id = ? AND {_AUTO_LIVE}",
            (auto_id,)) as cur:
        r = await cur.fetchone()
    if r is None:
        return {"ok": False, "error": "no live auto-allowed host with that id"}
    target = await _append_host(db, r["project_slug"], r["host"])
    await db.execute("UPDATE egress_auto_allow SET promoted_at = datetime('now') "
                     "WHERE id = ?", (auto_id,))
    await db.commit()
    return {"ok": True, "host": r["host"], "added_to": target}


async def allow_host(db: aiosqlite.Connection, slug: str, host: str) -> dict:
    """Operator allows a host directly — the override for an auto-deny (which
    has left the waiting queue). Trains the list like an approval, and closes
    any queue row for the pair."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return {"ok": False, "error": "host required"}
    slug = slug or GENERAL
    target = await _append_host(db, slug, host)
    await db.execute(
        "UPDATE egress_pending SET status = 'approved', decided_at = datetime('now') "
        "WHERE project_slug = ? AND host = ?", (slug, host))
    await db.commit()
    return {"ok": True, "host": host, "added_to": target}


async def remove_host(db: aiosqlite.Connection, slug: str, host: str) -> dict:
    """Operator revokes a standing allowlist entry from the row that holds it
    (`slug` is the row's own slug — a project, or GENERAL for the shared list)."""
    await ensure_general(db)
    row = await _row(db, slug)
    if row is None:
        return {"ok": False, "error": "no such policy"}
    hosts = json.loads(row["hosts"] or "[]")
    if host not in hosts:
        return {"ok": False, "error": "host is not on that list"}
    hosts.remove(host)
    await db.execute("UPDATE egress_policy SET hosts = ?, updated_at = datetime('now') "
                     "WHERE project_slug = ?", (json.dumps(sorted(hosts)), slug))
    await db.commit()
    return {"ok": True, "project": slug, "host": host}


async def allowlist(db: aiosqlite.Connection) -> list[dict]:
    """Every standing allowlist, grouped by the row that holds it, each entry
    tagged with where it came from: seed (config), reviewer (the triage
    reviewer approved it), operator (anything else on the list) or auto (a live
    auto-mode guess, with its expiry and reason)."""
    await ensure_general(db)
    seed = {h.lower() for h in settings.egress_seed_hosts}
    reviewed: set[tuple[str, str]] = set()
    async with db.execute(
            "SELECT detail, subject FROM triage_log WHERE item_kind = 'egress' "
            "AND action = 'approved' AND undone = 0") as cur:
        for r in await cur.fetchall():
            try:
                target = (json.loads(r["detail"] or "{}") or {}).get("added_to") or GENERAL
            except (ValueError, TypeError, AttributeError):
                target = GENERAL
            reviewed.add((target, r["subject"]))
    groups: dict[str, dict] = {}
    async with db.execute("SELECT project_slug, mode, hosts FROM egress_policy "
                          "ORDER BY project_slug = ? DESC, project_slug", (GENERAL,)) as cur:
        for r in await cur.fetchall():
            if r["mode"] != "allowlist":
                continue
            entries = []
            for h in json.loads(r["hosts"] or "[]"):
                src = ("seed" if r["project_slug"] == GENERAL and h.lower() in seed
                       else "reviewer" if (r["project_slug"], h) in reviewed
                       else "operator")
                entries.append({"host": h, "source": src})
            groups[r["project_slug"]] = {"project": r["project_slug"], "entries": entries}
    async with db.execute(
            f"SELECT id, project_slug, host, rule, reason, created_at, expires_at "
            f"FROM egress_auto_allow WHERE {_AUTO_LIVE} ORDER BY id DESC") as cur:
        for r in await cur.fetchall():
            g = groups.setdefault(r["project_slug"],
                                  {"project": r["project_slug"], "entries": []})
            g["entries"].append({"host": r["host"], "source": "auto", "id": r["id"],
                                 "rule": r["rule"], "reason": r["reason"],
                                 "created_at": r["created_at"],
                                 "expires_at": r["expires_at"]})
    for g in groups.values():
        g["entries"].sort(key=lambda e: (e["source"] != "auto", e["host"]))
    return list(groups.values())


async def note_denied(db: aiosqlite.Connection, slug: str, host: str) -> None:
    """Upsert the denied host into the approval queue (bump hit_count)."""
    await db.execute(
        "INSERT INTO egress_pending(project_slug, host) VALUES (?, ?) "
        "ON CONFLICT(project_slug, host) DO UPDATE SET "
        "hit_count = hit_count + 1, last_seen = datetime('now'), "
        # a re-hit re-queues an operator reject/dismiss (the long-standing
        # behaviour) but NOT an auto-mode deny or a revoked auto-allow: those
        # would otherwise bounce straight back into "waiting for you" on every
        # retry of the same bad host
        "status = CASE WHEN status IN ('rejected', 'dismissed') "
        "AND COALESCE(auto_verdict, '') NOT IN ('deny', 'revoked') "
        "THEN 'pending' ELSE status END",
        (slug, host))
    await db.commit()


async def record_event(db: aiosqlite.Connection, *, slug: str | None, host: str,
                       method: str | None = None, path: str | None = None,
                       bytes_out: int = 0, bytes_in: int = 0, verdict: str = "allow",
                       reason: str | None = None, op_id: str | None = None,
                       conversation_id: int | None = None) -> None:
    """Persist one egress event (feed + baseline) and stream it to the live view."""
    await db.execute(
        "INSERT INTO egress_events(project_slug, conversation_id, op_id, host, method, "
        "path, bytes_out, bytes_in, verdict, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (slug, conversation_id, op_id, host, method, path, bytes_out, bytes_in, verdict, reason))
    await db.commit()
    bus.publish(EGRESS_CHAN, {"type": "egress", "project": slug, "host": host,
                             "method": method, "path": path, "bytes_out": bytes_out,
                             "bytes_in": bytes_in, "verdict": verdict, "reason": reason})


# --- approval queue (trains the allowlist up) --------------------------------

async def _append_host(db: aiosqlite.Connection, slug: str, host: str) -> str:
    """Add a host to the allowlist that governs `slug`. A project that has its OWN
    allowlist policy trains up THAT list (kept isolated from other projects); a
    pure-default project (no policy row) trains up the shared GENERAL list — the
    intended shared-allowlist behaviour. Returns the slug of the row extended, so
    the caller/UI can show whether an approval widened the shared list."""
    await ensure_general(db)
    own = await _row(db, slug) if slug and slug != GENERAL else None
    target = slug if (own and own["mode"] == "allowlist") else GENERAL
    row = await _row(db, target)
    hosts = json.loads(row["hosts"] or "[]") if row else []
    if host not in hosts:
        hosts.append(host)
    if row is None:
        await db.execute("INSERT INTO egress_policy(project_slug, hosts) VALUES (?, ?)",
                         (target, json.dumps(sorted(hosts))))
    else:
        await db.execute("UPDATE egress_policy SET hosts = ?, updated_at = datetime('now') "
                         "WHERE project_slug = ?", (json.dumps(sorted(hosts)), target))
    await db.commit()
    return target


async def approve_host(db: aiosqlite.Connection, pending_id: int) -> dict:
    async with db.execute("SELECT project_slug, host, status FROM egress_pending WHERE id = ?",
                          (pending_id,)) as cur:
        r = await cur.fetchone()
    if r is None:
        return {"ok": False, "error": "no such pending host"}
    target = await _append_host(db, r["project_slug"], r["host"])
    await db.execute("UPDATE egress_pending SET status='approved', decided_at=datetime('now') "
                     "WHERE id = ?", (pending_id,))
    await db.commit()
    return {"ok": True, "host": r["host"], "added_to": target}


async def reject_host(db: aiosqlite.Connection, pending_id: int) -> dict:
    # clearing auto_verdict marks the row as a human decision, which egress
    # auto mode never overrides (egress_auto.judge)
    await db.execute("UPDATE egress_pending SET status='rejected', decided_at=datetime('now'), "
                     "auto_verdict=NULL WHERE id = ?", (pending_id,))
    await db.commit()
    return {"ok": True}


async def bulk_pending(db: aiosqlite.Connection, action: str,
                       slug: str | None = None) -> dict:
    """Decide every pending host at once — the queue reached hundreds and
    one-at-a-time was untenable. approve trains the allowlist exactly like the
    single path; reject and dismiss only change status. dismiss records that
    the queue was cleared without a verdict — like reject, a host that is hit
    again re-queues."""
    if action not in ("approve", "reject", "dismiss"):
        return {"ok": False, "error": "action must be approve|reject|dismiss"}
    rows = await list_pending(db, slug)
    if action == "approve":
        for r in rows:
            await _append_host(db, r["project_slug"], r["host"])
    status = {"approve": "approved", "reject": "rejected",
              "dismiss": "dismissed"}[action]
    await db.executemany(
        "UPDATE egress_pending SET status=?, decided_at=datetime('now'), "
        "auto_verdict=NULL WHERE id=?",
        [(status, r["id"]) for r in rows])
    await db.commit()
    return {"ok": True, "done": len(rows)}


async def list_pending(db: aiosqlite.Connection, slug: str | None = None) -> list[dict]:
    q = ("SELECT id, project_slug, host, hit_count, first_seen, last_seen, status, "
         "triage_verdict, triage_reason, auto_verdict, auto_reason FROM egress_pending "
         "WHERE status='pending'")
    args: tuple = ()
    if slug:
        q += " AND project_slug = ?"
        args = (slug,)
    # reviewer-flagged hosts first — those are the ones actually waiting on a human
    q += " ORDER BY (triage_verdict = 'flag') DESC, last_seen DESC"
    async with db.execute(q, args) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def set_policy(db: aiosqlite.Connection, slug: str, *, mode: str = "allowlist",
                     inherit_general: bool = True, hosts: list[str] | None = None) -> dict:
    """Create/replace a project's scoped policy (the 'sensitive project' path)."""
    if mode not in ("allowlist", "denylist", "denyall"):
        return {"ok": False, "error": "mode must be allowlist|denylist|denyall"}
    await db.execute(
        "INSERT INTO egress_policy(project_slug, mode, inherit_general, hosts) VALUES (?,?,?,?) "
        "ON CONFLICT(project_slug) DO UPDATE SET mode=excluded.mode, "
        "inherit_general=excluded.inherit_general, hosts=excluded.hosts, updated_at=datetime('now')",
        (slug, mode, 1 if inherit_general else 0, json.dumps(sorted(hosts or []))))
    await db.commit()
    return {"ok": True, "slug": slug, "mode": mode}


# --- auto-cut (called by backend/anomaly.py) ---------------------------------

def is_cut(slug: str, host: str) -> bool:
    return (slug, host) in _cut or (GENERAL, host) in _cut


def mark_cut(slug: str | None, host: str) -> None:
    _cut.add((slug or GENERAL, host))


def clear_cut(slug: str | None, host: str) -> None:
    _cut.discard((slug or GENERAL, host))


# --- per-project secret grants (B1) ------------------------------------------
# The proxy injects a {{secret:X}} into an outbound request only if the project
# holds a granted row for X — so a compromised project can't reach for every key
# the operator owns. This is the Layer-2 blast-radius control for wire injection.

async def may_use_secret(db: aiosqlite.Connection, slug: str, name: str) -> bool:
    async with db.execute(
            "SELECT 1 FROM project_secret_grants WHERE project_slug = ? AND "
            "secret_name = ? AND status = 'granted'", (slug, name.upper())) as cur:
        return await cur.fetchone() is not None


async def grant_secret(db: aiosqlite.Connection, slug: str, name: str,
                       status: str = "granted") -> dict:
    await db.execute(
        "INSERT INTO project_secret_grants(project_slug, secret_name, status) VALUES (?,?,?) "
        "ON CONFLICT(project_slug, secret_name) DO UPDATE SET status = excluded.status",
        (slug, name.upper(), status))
    await db.commit()
    return {"ok": True, "project": slug, "secret": name.upper(), "status": status}


async def revoke_secret(db: aiosqlite.Connection, slug: str, name: str) -> dict:
    return await grant_secret(db, slug, name, status="revoked")


async def project_secrets(db: aiosqlite.Connection, slug: str) -> list[dict]:
    async with db.execute(
            "SELECT secret_name, status FROM project_secret_grants WHERE project_slug = ? "
            "ORDER BY secret_name", (slug,)) as cur:
        return [dict(r) for r in await cur.fetchall()]
