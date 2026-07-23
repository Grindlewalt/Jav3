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
_context: dict = {"project": None, "op_id": None, "conversation_id": None}


def set_context(project: str | None, op_id: str | None = None,
                conversation_id: int | None = None) -> None:
    _context.update(project=project, op_id=op_id, conversation_id=conversation_id)


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
    return "deny", "host not on the allowlist (queued for approval)"


async def note_denied(db: aiosqlite.Connection, slug: str, host: str) -> None:
    """Upsert the denied host into the approval queue (bump hit_count)."""
    await db.execute(
        "INSERT INTO egress_pending(project_slug, host) VALUES (?, ?) "
        "ON CONFLICT(project_slug, host) DO UPDATE SET "
        "hit_count = hit_count + 1, last_seen = datetime('now'), "
        "status = CASE WHEN status = 'rejected' THEN 'pending' ELSE status END",
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
    await db.execute("UPDATE egress_pending SET status='rejected', decided_at=datetime('now') "
                     "WHERE id = ?", (pending_id,))
    await db.commit()
    return {"ok": True}


async def list_pending(db: aiosqlite.Connection, slug: str | None = None) -> list[dict]:
    q = ("SELECT id, project_slug, host, hit_count, first_seen, last_seen, status "
         "FROM egress_pending WHERE status='pending'")
    args: tuple = ()
    if slug:
        q += " AND project_slug = ?"
        args = (slug,)
    q += " ORDER BY last_seen DESC"
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
