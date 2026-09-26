"""Per-project egress policy — the fine-grained half of Layer 3.

nftables gives the coarse floor (drop LAN, force DNS through the host resolver,
redirect 80/443 to the host proxy, drop everything else). THIS module is what
the proxy consults per request to decide allow / deny / cut on the *hostname*,
and it owns the approval queue that trains the allowlists up.

The model (DESIGN-BOXES (c)/(d), since 2026-09-26): every project runs under a
security PROFILE (backend/profiles.py) that carries the shared baseline — a
default verdict, allow and deny lists, or the network off entirely — and holds
its OWN allow and deny lists on top (`egress_policy.hosts` / `.deny_hosts`).
Approvals, reviewer approvals and `allow_host` always write the PROJECT's own
list, never a shared one; profile lists are edited only by the operator.
The pre-profiles modes (allowlist / denylist / denyall, inherit_general) were
migrated into the builtin profiles Default / Scoped / Open / Offline with
identical verdicts.

A new/unapproved host under a deny-by-default profile is DENIED and queued —
that is routine, not an alarm. Only exfil-shaped behaviour (backend/anomaly.py)
raises a security_event and a `cut`, which this module records so the proxy
refuses the host immediately.
"""
import json

import aiosqlite

from . import bus, profiles
from .config import settings

GENERAL = "__general__"          # the unattributed slug (judged by the Default profile)
EGRESS_CHAN = "egress"           # bus channel the live Network view subscribes to

# The shared box's proxy listener sees raw guest requests with no op_id, so its
# egress is attributed to the operation currently driving the shared guest. The
# broker sets this on register_turn (innermost/most-recent wins; nested turns
# share the project). A plain module global — not a contextvar — because the
# proxy runs on a different asyncio task than the turn. Boxes of their own
# (project / service / builder, DESIGN-BOXES "Proxy attribution") are
# attributed by the proxy listener they reach instead, and never read this.
#
# It is a STACK because turns overlap in both directions: a spawn_agent child
# registers while its parent is still open, and several chats can drive the one
# guest at once. So a turn ending has to hand attribution back to whatever is
# still running rather than blanking it — and, when nothing is, actually clear.
# Until 2026-08-10 it never cleared at all: `release_turn` dropped the envelope
# and left the finished project's slug in place, so anything the guest did
# afterwards (a process outliving its run_code call, a straggling connection)
# was policed under the last project to have run. Unattributed traffic now
# falls back to the Default profile, which is what the very first request
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


def context_matching(pred) -> dict | None:
    """The innermost live turn entry for which `pred(entry)` holds (the proxy
    asks for the ops bound to a project box), for op/conversation attribution
    of that box's traffic. The PROJECT of a box's traffic never comes from
    here — only from the box."""
    for e in reversed(_stack):
        if pred(e):
            return dict(e)
    return None

# (project_slug, host) pairs auto-cut this process. The nft drop (Pi-side) is
# the hard block; this in-memory set is what the proxy checks synchronously so a
# cut takes effect on the very next request without a DB round-trip.
_cut: set[tuple[str, str]] = set()


def _norm(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


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
            "SELECT project_slug, mode, inherit_general, hosts, deny_hosts "
            "FROM egress_policy WHERE project_slug = ?", (slug,)) as cur:
        r = await cur.fetchone()
    return dict(r) if r else None


async def ensure_general(db: aiosqlite.Connection) -> None:
    """Kept for callers that predate profiles: the shared baseline is now the
    `Default` profile's allow list, created by the profiles migration (which
    also keeps the old `__general__` row for the audit trail)."""
    await profiles.ensure_migrated(db)


def is_unattributed(slug: str | None) -> bool:
    return not slug or slug == GENERAL


async def project_lists(db: aiosqlite.Connection,
                        slug: str | None) -> tuple[list[str], list[str]]:
    """(allow, deny) the project itself holds. Unattributed traffic has none:
    it is judged by the Default profile only."""
    if is_unattributed(slug):
        return [], []
    own = await _row(db, slug)
    if own is None:
        return [], []
    return json.loads(own["hosts"] or "[]"), json.loads(own.get("deny_hosts") or "[]")


def _dedupe(hosts: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for h in hosts:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


async def get_policy(db: aiosqlite.Connection, slug: str | None) -> dict:
    """The effective policy for a project (DESIGN-BOXES (c)):
    {slug, profile:{id,name,default,...}, project_allow, project_deny,
     effective_allow, effective_deny}
    plus the pre-profiles keys (mode, inherit_general, hosts, effective,
    source) derived from them, for callers and clients that predate the new
    shape. `effective_allow` is what an explicit allow comes from (project +
    profile lists); the profile's `default` decides every other host."""
    prof = await profiles.for_slug(db, slug)
    p_allow, p_deny = await project_lists(db, slug)
    net_off = bool(prof["network_off"])
    eff_allow = [] if net_off else _dedupe(p_allow + prof["allow_hosts"])
    eff_deny = _dedupe(p_deny + prof["deny_hosts"])
    default = "deny" if net_off else prof["default_verdict"]
    mode = "denyall" if net_off else ("denylist" if default == "allow" else "allowlist")
    is_default = bool(prof["builtin"]) and prof["name"] == profiles.DEFAULT
    return {
        "slug": slug,
        "profile": {"id": prof["id"], "name": prof["name"], "default": default,
                    "network_off": net_off, "builtin": bool(prof["builtin"])},
        "project_allow": p_allow, "project_deny": p_deny,
        "effective_allow": eff_allow, "effective_deny": eff_deny,
        # --- the pre-profiles view (read-only compatibility)
        "mode": mode, "inherit_general": 1 if is_default else 0,
        "hosts": p_allow if mode == "allowlist" else p_deny,
        "effective": eff_deny if mode == "denylist" else eff_allow,
        "source": "general" if (is_default and not p_allow and not p_deny) else "project",
    }


# The one deny reason egress auto mode may act on: a deny-by-default profile
# meeting a host nobody has decided about. Every other deny (network off, a
# deny list, cut) is a standing decision the guesser must never second-guess.
NOT_LISTED = "host not on the allowlist (queued for approval)"


async def decide(db: aiosqlite.Connection, slug: str | None, host: str) -> tuple[str, str]:
    """(verdict, reason) for one host. verdict in {allow, deny, cut}.

    Order (DESIGN-BOXES (c); deny beats allow at every level):
      cut -> [network off] -> project deny -> profile deny -> project allow ->
      profile allow -> live auto-allow -> the profile's default.
    A profile with the network off denies everything a cut does not already
    refuse. Unattributed traffic (slug None / __general__) is judged by the
    Default profile only: no project lists, no auto-allow."""
    host = _norm(host)
    if (slug, host) in _cut or (GENERAL, host) in _cut:
        return "cut", "host auto-cut after an anomaly"
    prof = await profiles.for_slug(db, slug)
    if prof["network_off"]:
        return "deny", f"egress disabled for this project ({prof['name']} profile)"
    p_allow, p_deny = await project_lists(db, slug)
    if _host_matches(host, p_deny):
        return "deny", "host on the project denylist"
    if _host_matches(host, prof["deny_hosts"]):
        return "deny", f"host on the {prof['name']} profile denylist"
    if _host_matches(host, p_allow):
        return "allow", "host on the project allowlist"
    if _host_matches(host, prof["allow_hosts"]):
        return "allow", f"host on the {prof['name']} profile allowlist"
    if not is_unattributed(slug):
        auto = await active_auto(db, slug, host)
        if auto:
            return "allow", f"auto-allowed until {auto['expires_at']} UTC: {auto['reason']}"
    if prof["default_verdict"] == "allow":
        return "allow", f"allow-by-default ({prof['name']} profile)"
    return "deny", NOT_LISTED


async def decide_service(db: aiosqlite.Connection, slug: str | None,
                         service_id: int | None, host: str) -> tuple[str, str]:
    """Service-box traffic (DESIGN-BOXES (a) Egress): ALWAYS deny-by-default,
    whatever the profile's default says. Allowed = the approved service's own
    `egress_hosts` minus the project and profile deny lists (network off still
    denies all). No auto mode and no queue training: the proxy never queues a
    service denial; widening means editing the service. A box shared by
    several services (per_project / shared placement) gets the union of its
    approved services' hosts."""
    host = _norm(host)
    if (slug, host) in _cut or (GENERAL, host) in _cut:
        return "cut", "host auto-cut after an anomaly"
    prof = await profiles.for_slug(db, slug)
    if prof["network_off"]:
        return "deny", f"egress disabled for this project ({prof['name']} profile)"
    _allow, p_deny = await project_lists(db, slug)
    if _host_matches(host, p_deny):
        return "deny", "host on the project denylist"
    if _host_matches(host, prof["deny_hosts"]):
        return "deny", f"host on the {prof['name']} profile denylist"
    if service_id is not None:
        q, args = ("SELECT egress_hosts FROM services WHERE id = ? AND status = 'approved'",
                   (service_id,))
    elif slug:
        q, args = ("SELECT egress_hosts FROM services WHERE project_slug = ? "
                   "AND status = 'approved' AND placement = 'per_project'", (slug,))
    else:
        q, args = ("SELECT egress_hosts FROM services WHERE status = 'approved' "
                   "AND placement = 'shared'", ())
    allowed: list[str] = []
    async with db.execute(q, args) as cur:
        for r in await cur.fetchall():
            try:
                allowed += [str(h) for h in json.loads(r["egress_hosts"] or "[]")]
            except (TypeError, ValueError):
                continue
    if _host_matches(host, allowed):
        return "allow", "host in the approved service's egress_hosts"
    return "deny", "service egress: host not in the approved service's egress_hosts"


# --- auto-allowed hosts (egress auto mode) -------------------------------------
# Deliberately NOT part of get_policy()['effective_allow']: the triage reviewer
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
    """Operator keeps a guess: it moves onto the project's own allowlist (same
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


UNATTRIBUTED = ("this request came from the shared box with no project attached: "
                "choose the project it belongs to")


async def allow_host(db: aiosqlite.Connection, slug: str, host: str) -> dict:
    """Operator allows a host directly — the override for an auto-deny (which
    has left the waiting queue). Writes the PROJECT's own list, and closes any
    queue row for the pair."""
    host = _norm(host)
    if not host:
        return {"ok": False, "error": "host required"}
    if is_unattributed(slug):
        return {"ok": False, "error": UNATTRIBUTED, "needs_project": True}
    target = await _append_host(db, slug, host)
    await db.execute(
        "UPDATE egress_pending SET status = 'approved', decided_at = datetime('now') "
        "WHERE project_slug = ? AND host = ?", (slug, host))
    await db.commit()
    await note_approved(db, slug, host)
    return {"ok": True, "host": host, "added_to": target}


def _profile_ref(slug: str) -> int | str | None:
    """'profile:<id>' -> id; GENERAL -> 'Default'; else None (a project)."""
    if slug == GENERAL:
        return profiles.DEFAULT
    if slug.startswith("profile:"):
        try:
            return int(slug.split(":", 1)[1])
        except ValueError:
            return None
    return None


async def remove_host(db: aiosqlite.Connection, slug: str, host: str,
                      which: str = "allow") -> dict:
    """Operator removes a standing entry from the list that holds it. `slug` is
    the list's own key: a project slug, `profile:<id>` for a profile's list,
    or GENERAL for the Default profile's (the successor of the old shared
    list). A profile edit is a `profile_changed` event like any other."""
    col = "deny_hosts" if which == "deny" else "hosts"
    ref = _profile_ref(slug or "")
    if ref is not None:
        prof = (await profiles.by_name(db, ref) if isinstance(ref, str)
                else await profiles.get(db, ref))
        if prof is None:
            return {"ok": False, "error": "no such profile"}
        key = "deny_hosts" if which == "deny" else "allow_hosts"
        hosts = list(prof[key])
        if host not in hosts:
            return {"ok": False, "error": "host is not on that list"}
        hosts.remove(host)
        await profiles.set_hosts(db, prof["id"], **{("deny" if which == "deny" else "allow"): hosts})
        return {"ok": True, "project": slug, "profile": prof["name"], "host": host}
    row = await _row(db, slug)
    if row is None:
        return {"ok": False, "error": "no such policy"}
    hosts = json.loads(row[col] or "[]")
    if host not in hosts:
        return {"ok": False, "error": "host is not on that list"}
    hosts.remove(host)
    await db.execute(f"UPDATE egress_policy SET {col} = ?, updated_at = datetime('now') "
                     "WHERE project_slug = ?", (json.dumps(sorted(hosts)), slug))
    await db.commit()
    return {"ok": True, "project": slug, "host": host}


async def promote_to_profile(db: aiosqlite.Connection, slug: str, host: str,
                             profile_id: int | None = None, which: str = "allow",
                             actor: str = "operator") -> dict:
    """"Promote to profile": move a host from the project's own list onto a
    profile's list of the same kind (default: the project's own profile), in
    one step. The profile edit is a `profile_changed` event; the project entry
    is removed so the host lives in exactly one place."""
    host = _norm(host)
    if not host or is_unattributed(slug):
        return {"ok": False, "error": "a project and a host are required"}
    prof = (await profiles.get(db, profile_id) if profile_id is not None
            else await profiles.for_slug(db, slug))
    if prof is None:
        return {"ok": False, "error": "no such profile"}
    key = "deny_hosts" if which == "deny" else "allow_hosts"
    if host not in prof[key]:
        try:
            await profiles.set_hosts(db, prof["id"], actor=actor,
                                     **{("deny" if which == "deny" else "allow"):
                                        [*prof[key], host]})
        except profiles.ProfileError as e:
            return {"ok": False, "error": str(e)}
    removed = (await remove_host(db, slug, host, which=which))["ok"]
    return {"ok": True, "host": host, "list": "deny" if which == "deny" else "allow",
            "profile": {"id": prof["id"], "name": prof["name"]},
            "removed_from_project": removed}


async def allowlist(db: aiosqlite.Connection) -> list[dict]:
    """Every standing list, grouped by project, then by profile. Each group:
    {project, kind:'project'|'profile', profile:{id,name,default}, entries,
    deny}. Project groups key on the slug; profile groups on `profile:<id>`,
    except the Default profile, which keeps the old shared list's key
    GENERAL. Each allow entry is tagged with where it came from: seed
    (config), reviewer (the triage reviewer approved it), operator (anything
    else on the list) or auto (a live auto-mode guess, with its expiry)."""
    await profiles.ensure_migrated(db)
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

    async def _project_group(slug: str) -> dict:
        if slug not in groups:
            prof = await profiles.for_slug(db, slug)
            groups[slug] = {"project": slug, "kind": "project",
                            "profile": {"id": prof["id"], "name": prof["name"],
                                        "default": prof["default_verdict"]},
                            "entries": [], "deny": []}
        return groups[slug]

    async with db.execute("SELECT project_slug, hosts, deny_hosts FROM egress_policy "
                          "WHERE project_slug != ? ORDER BY project_slug",
                          (GENERAL,)) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        allow = json.loads(r["hosts"] or "[]")
        deny = json.loads(r["deny_hosts"] or "[]")
        if not allow and not deny:
            continue
        g = await _project_group(r["project_slug"])
        for h in allow:
            g["entries"].append({"host": h, "source": "reviewer"
                                 if (r["project_slug"], h) in reviewed else "operator"})
        g["deny"] = sorted(deny)
    async with db.execute(
            f"SELECT id, project_slug, host, rule, reason, created_at, expires_at "
            f"FROM egress_auto_allow WHERE {_AUTO_LIVE} ORDER BY id DESC") as cur:
        autos = [dict(r) for r in await cur.fetchall()]
    for r in autos:
        g = await _project_group(r["project_slug"])
        g["entries"].append({"host": r["host"], "source": "auto", "id": r["id"],
                             "rule": r["rule"], "reason": r["reason"],
                             "created_at": r["created_at"],
                             "expires_at": r["expires_at"]})
    for p in await profiles.list_all(db):
        is_default = p["builtin"] and p["name"] == profiles.DEFAULT
        key = GENERAL if is_default else f"profile:{p['id']}"
        entries = [{"host": h, "source": ("seed" if is_default and h.lower() in seed
                                          else "reviewer" if (key, h) in reviewed
                                          else "operator")}
                   for h in p["allow_hosts"]]
        groups[key] = {"project": key, "kind": "profile",
                       "profile": {"id": p["id"], "name": p["name"],
                                   "default": p["default_verdict"]},
                       "entries": entries, "deny": list(p["deny_hosts"]),
                       "projects": p.get("projects", [])}
    for g in groups.values():
        g["entries"].sort(key=lambda e: (e["source"] != "auto", e["host"]))
    return list(groups.values())


async def note_denied(db: aiosqlite.Connection, slug: str, host: str,
                      box_id: str | None = None) -> None:
    """Upsert the denied host into the approval queue (bump hit_count)."""
    await db.execute(
        "INSERT INTO egress_pending(project_slug, host, box_id) VALUES (?, ?, ?) "
        "ON CONFLICT(project_slug, host) DO UPDATE SET "
        "hit_count = hit_count + 1, last_seen = datetime('now'), "
        "box_id = COALESCE(excluded.box_id, box_id), "
        # a re-hit re-queues an operator reject/dismiss (the long-standing
        # behaviour) but NOT an auto-mode deny or a revoked auto-allow: those
        # would otherwise bounce straight back into "waiting for you" on every
        # retry of the same bad host
        "status = CASE WHEN status IN ('rejected', 'dismissed') "
        "AND COALESCE(auto_verdict, '') NOT IN ('deny', 'revoked') "
        "THEN 'pending' ELSE status END",
        (slug, host, box_id))
    await db.commit()


async def record_event(db: aiosqlite.Connection, *, slug: str | None, host: str,
                       method: str | None = None, path: str | None = None,
                       bytes_out: int = 0, bytes_in: int = 0, verdict: str = "allow",
                       reason: str | None = None, op_id: str | None = None,
                       conversation_id: int | None = None, peer_ip: str | None = None,
                       peer_port: int | None = None, box_id: str | None = None,
                       service_id: int | None = None) -> None:
    """Persist one egress event (feed + baseline) and stream it to the live
    view. peer_ip/peer_port are the guest end of the proxied connection and
    box_id/service_id who it came from: the process view (WP4) joins a proxy
    row to a guest socket on (box_id, peer_port)."""
    await db.execute(
        "INSERT INTO egress_events(project_slug, conversation_id, op_id, host, method, "
        "path, bytes_out, bytes_in, verdict, reason, peer_ip, peer_port, box_id, "
        "service_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (slug, conversation_id, op_id, host, method, path, bytes_out, bytes_in, verdict,
         reason, peer_ip, peer_port, box_id, service_id))
    await db.commit()
    bus.publish(EGRESS_CHAN, {"type": "egress", "project": slug, "host": host,
                             "method": method, "path": path, "bytes_out": bytes_out,
                             "bytes_in": bytes_in, "verdict": verdict, "reason": reason,
                             "box_id": box_id, "service_id": service_id})


# --- approval queue (trains the PROJECT's allowlist up) -----------------------

async def _append_host(db: aiosqlite.Connection, slug: str, host: str) -> str:
    """Add a host to the PROJECT's own allow list (DESIGN-BOXES (c): approvals
    always write the project list; the shared baseline lives on the profile and
    is edited only by the operator). An explicit approval also lifts the host
    off the project's own deny list, or deny-beats-allow would void it.
    Returns the slug of the list extended. Refuses an unattributed slug."""
    if is_unattributed(slug):
        raise ValueError(UNATTRIBUTED)
    host = _norm(host)
    row = await _row(db, slug)
    hosts = json.loads(row["hosts"] or "[]") if row else []
    deny = json.loads(row["deny_hosts"] or "[]") if row else []
    if host not in hosts:
        hosts.append(host)
    deny = [h for h in deny if h != host]
    if row is None:
        await db.execute("INSERT INTO egress_policy(project_slug, hosts) VALUES (?, ?)",
                         (slug, json.dumps(sorted(hosts))))
    else:
        await db.execute("UPDATE egress_policy SET hosts = ?, deny_hosts = ?, "
                         "updated_at = datetime('now') WHERE project_slug = ?",
                         (json.dumps(sorted(hosts)), json.dumps(sorted(deny)), slug))
    await db.commit()
    return slug


APPROVED_BY = {"operator": "approved later by you",
               "reviewer": "approved later by the triage reviewer"}


async def note_approved(db: aiosqlite.Connection, slug: str | None, host: str,
                        by: str = "operator") -> None:
    """Log the approval as its own event. egress_events is a request log: the
    request that queued a host was logged `deny` at the time and that row stays
    true, so without this the Network page's "Recent decisions" went on showing
    only the deny after the host had been approved. The verdicts (`approved`,
    `reviewer_approved`) are never counted as traffic: the summary and the
    anomaly baseline read allow/deny only."""
    await record_event(db, slug=slug, host=host,
                       verdict="reviewer_approved" if by == "reviewer" else "approved",
                       reason=APPROVED_BY.get(by, f"approved later by {by}"))


async def approve_host(db: aiosqlite.Connection, pending_id: int,
                       by: str = "operator", project: str | None = None) -> dict:
    """Approve a queued host onto its project's list. A row queued by
    unattributed shared-box traffic has no project: the operator names one
    (`project`), and the row is re-homed to it; without one this refuses."""
    async with db.execute("SELECT project_slug, host, status FROM egress_pending WHERE id = ?",
                          (pending_id,)) as cur:
        r = await cur.fetchone()
    if r is None:
        return {"ok": False, "error": "no such pending host"}
    slug = r["project_slug"]
    if is_unattributed(slug):
        if is_unattributed(project):
            return {"ok": False, "error": UNATTRIBUTED, "needs_project": True}
        slug = project
    target = await _append_host(db, slug, r["host"])
    await db.execute("UPDATE egress_pending SET status='approved', decided_at=datetime('now') "
                     "WHERE id = ?", (pending_id,))
    await db.commit()
    if r["status"] != "approved":
        await note_approved(db, slug, r["host"], by)
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
    one-at-a-time was untenable. approve trains each row's PROJECT list
    exactly like the single path; unattributed rows are skipped by approve
    (they need a project named) and reported as `skipped`. reject and dismiss
    only change status; dismiss records that the queue was cleared without a
    verdict — like reject, a host that is hit again re-queues."""
    if action not in ("approve", "reject", "dismiss"):
        return {"ok": False, "error": "action must be approve|reject|dismiss"}
    rows = await list_pending(db, slug)
    skipped = 0
    if action == "approve":
        keep = []
        for r in rows:
            if is_unattributed(r["project_slug"]):
                skipped += 1
                continue
            await _append_host(db, r["project_slug"], r["host"])
            keep.append(r)
        rows = keep
    status = {"approve": "approved", "reject": "rejected",
              "dismiss": "dismissed"}[action]
    await db.executemany(
        "UPDATE egress_pending SET status=?, decided_at=datetime('now'), "
        "auto_verdict=NULL WHERE id=?",
        [(status, r["id"]) for r in rows])
    await db.commit()
    if action == "approve":
        for r in rows:
            await note_approved(db, r["project_slug"], r["host"])
    return {"ok": True, "done": len(rows), "skipped": skipped}


async def list_pending(db: aiosqlite.Connection, slug: str | None = None) -> list[dict]:
    q = ("SELECT id, project_slug, host, hit_count, first_seen, last_seen, status, "
         "triage_verdict, triage_reason, auto_verdict, auto_reason, box_id "
         "FROM egress_pending WHERE status='pending'")
    args: tuple = ()
    if slug:
        q += " AND project_slug = ?"
        args = (slug,)
    # reviewer-flagged hosts first — those are the ones actually waiting on a human
    q += " ORDER BY (triage_verdict = 'flag') DESC, last_seen DESC"
    async with db.execute(q, args) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def set_lists(db: aiosqlite.Connection, slug: str, *, allow: list[str] | None = None,
                    deny: list[str] | None = None) -> dict:
    """Replace a project's OWN allow and/or deny list (PUT /api/egress/policy)."""
    if is_unattributed(slug):
        return {"ok": False, "error": "the unattributed list is the Default profile's: "
                                      "edit it on the profile"}
    try:
        allow_n = profiles.norm_hosts(allow) if allow is not None else None
        deny_n = profiles.norm_hosts(deny) if deny is not None else None
    except profiles.ProfileError as e:
        return {"ok": False, "error": str(e)}
    row = await _row(db, slug)
    if row is None:
        await db.execute("INSERT INTO egress_policy(project_slug, hosts, deny_hosts) "
                         "VALUES (?, ?, ?)", (slug, json.dumps(allow_n or []),
                                              json.dumps(deny_n or [])))
    else:
        cur_allow = json.loads(row["hosts"] or "[]")
        cur_deny = json.loads(row["deny_hosts"] or "[]")
        await db.execute(
            "UPDATE egress_policy SET hosts = ?, deny_hosts = ?, updated_at = datetime('now') "
            "WHERE project_slug = ?",
            (json.dumps(allow_n if allow_n is not None else cur_allow),
             json.dumps(deny_n if deny_n is not None else cur_deny), slug))
    await db.commit()
    return {"ok": True, **await get_policy(db, slug)}


async def set_policy(db: aiosqlite.Connection, slug: str, *, mode: str = "allowlist",
                     inherit_general: bool = True, hosts: list[str] | None = None) -> dict:
    """The pre-profiles call, kept as a translation: the old mode picks the
    builtin profile that reproduces it (allowlist+inherit -> Default,
    allowlist -> Scoped, denylist -> Open, denyall -> Offline) and `hosts`
    becomes the project's own allow (allowlist) or deny list. A profile move is
    a `profile_changed` event."""
    if mode not in ("allowlist", "denylist", "denyall"):
        return {"ok": False, "error": "mode must be allowlist|denylist|denyall"}
    name = profiles.legacy_profile_name(mode, inherit_general)
    prof = await profiles.by_name(db, name)
    await profiles.assign(db, slug, prof["id"], require_project=False)
    allow, deny = profiles.legacy_lists(mode, sorted(hosts or []))
    await db.execute("UPDATE egress_policy SET hosts = ?, deny_hosts = ?, "
                     "updated_at = datetime('now') WHERE project_slug = ?",
                     (json.dumps(sorted(allow)), json.dumps(sorted(deny)), slug))
    await db.commit()
    return {"ok": True, "slug": slug, "mode": mode, "profile": name}


# --- auto-cut (called by backend/anomaly.py) ---------------------------------

def is_cut(slug: str, host: str) -> bool:
    return (slug, host) in _cut or (GENERAL, host) in _cut


def mark_cut(slug: str | None, host: str) -> None:
    _cut.add((slug or GENERAL, host))


def clear_cut(slug: str | None, host: str) -> None:
    _cut.discard((slug or GENERAL, host))


# --- secret grants (B1; profiles (d)) -----------------------------------------
# A project may use secret X if its PROFILE lists X or the project holds a
# granted row for X — and never if the project holds a revoked row for X (a
# per-project revoke wins over the profile). This one rule governs both paths:
# wire injection in the proxy (inject_secrets) and web_read's URL substitution
# (secrets.substitute_url via webtools.read).

async def granted_secrets(db: aiosqlite.Connection, slug: str | None) -> set[str]:
    """Secret NAMES the project may use. Unattributed (None / __general__):
    the Default profile's list only."""
    prof = await profiles.for_slug(db, slug)
    names = {n.upper() for n in prof["secrets"]}
    if is_unattributed(slug):
        return names
    async with db.execute("SELECT secret_name, status FROM project_secret_grants "
                          "WHERE project_slug = ?", (slug,)) as cur:
        for r in await cur.fetchall():
            n = r["secret_name"].upper()
            if r["status"] == "granted":
                names.add(n)
            elif r["status"] == "revoked":
                names.discard(n)
    return names


async def may_use_secret(db: aiosqlite.Connection, slug: str, name: str) -> bool:
    return name.upper() in await granted_secrets(db, slug)


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
