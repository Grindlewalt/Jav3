"""Security profiles (DESIGN-BOXES.md (d); the WP2 half of docs/boxes-contract.md).

A profile is the shared baseline a project runs under: its egress default and
lists, the secrets every project under it may use, whether the triage reviewer
may handle its items, and how its boxes are built. A project points at one via
`projects.profile_id` (NULL = the builtin `Default`). The project's OWN
allow/deny lists stay in `egress_policy` (`hosts` = allow, `deny_hosts` = deny)
and are the only thing approvals ever train (egress.py).

Four builtins, created by the one-time migration below from today's per-project
modes: `Default` (the old `__general__` list, deny-by-default), `Scoped`
(allowlist without the shared list), `Open` (allow-by-default, the old denylist
mode) and `Offline` (the old denyall). Builtins can be edited but never deleted
or renamed (boxes.project_profile finds `Default` by name).

Profile resolution for a slug (for_slug):
  * None / `__general__` (unattributed shared-box traffic) -> Default.
  * a projects row -> its profile_id, NULL -> Default.
  * no projects row but an egress_policy row (a policy set for a slug that is
    not a project: tests, a hard-deleted project) -> the builtin its row's
    legacy `mode` maps to, so such a row keeps its exact verdicts too.
  * anything else -> Default.
"""
import json
import re

import aiosqlite

from . import bus
from .config import settings

GENERAL = "__general__"
SECURITY_CHAN = "security"

PLACEMENTS = ("per_service", "per_project", "shared")
RUNTIMES = ("kvm", "docker")
VERDICTS = ("deny", "allow")

DEFAULT, SCOPED, OPEN, OFFLINE = "Default", "Scoped", "Open", "Offline"
BUILTIN_NAMES = (DEFAULT, SCOPED, OPEN, OFFLINE)

# the fields a profile carries (the API row shape, minus id/builtin/projects)
_JSON_FIELDS = ("allow_hosts", "deny_hosts", "secrets")
_BOOL_FIELDS = ("network_off", "auto_handle", "separate_box", "allow_services",
                "allow_package_requests")
FIELDS = ("name", "default_verdict", "network_off", "allow_hosts", "deny_hosts",
          "secrets", "auto_handle", "separate_box", "box_image", "box_mem_mb",
          "box_runtime", "allow_services", "allow_package_requests",
          "service_placement")

_HOST_RE = re.compile(r"^[a-z0-9_]([a-z0-9_.-]{0,252})$")
_SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_VARIANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ProfileError(ValueError):
    """Validation failure; the API maps it to 400 (or 409 via .status)."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


# --- row helpers -----------------------------------------------------------------

def decode(row) -> dict:
    d = dict(row)
    for f in _JSON_FIELDS:
        try:
            v = json.loads(d.get(f) or "[]")
        except (TypeError, ValueError):
            v = []
        d[f] = [str(x) for x in v] if isinstance(v, list) else []
    for f in _BOOL_FIELDS + ("builtin",):
        if f in d:
            d[f] = bool(d[f])
    return d


def norm_hosts(hosts, *, strict: bool = True) -> list[str]:
    """Lower-cased, trailing-dot-free, de-duplicated, sorted. `strict` refuses
    anything that is not a bare hostname (a URL, a path, a wildcard, spaces)."""
    out: set[str] = set()
    for h in hosts or []:
        h = str(h or "").strip().lower().rstrip(".")
        if not h:
            continue
        if strict and (not _HOST_RE.match(h) or ".." in h):
            raise ProfileError(f"not a bare hostname: {h[:80]!r}")
        out.add(h)
    return sorted(out)


def norm_secrets(names) -> list[str]:
    out: set[str] = set()
    for n in names or []:
        n = str(n or "").strip().upper()
        if not n:
            continue
        if not _SECRET_RE.match(n):
            raise ProfileError(f"not a secret name: {n[:64]!r}")
        out.add(n)
    return sorted(out)


# --- migration -------------------------------------------------------------------

_migrated: set[str] = set()          # db paths known to be migrated (this process)


def _builtin_rows(general_hosts: list[str]) -> list[dict]:
    """The four builtins. `service_placement` and `box_runtime` are set
    EXPLICITLY (operator decision 0.1: never a default). auto_handle is ON for
    all four so the reviewer keeps handling exactly what it handled before the
    migration; the global reviewer switch stays the master kill."""
    base = {"network_off": 0, "deny_hosts": [], "secrets": [], "auto_handle": 1,
            "separate_box": 0, "box_image": "main", "box_mem_mb": None,
            "box_runtime": "kvm", "allow_services": 0, "allow_package_requests": 0,
            "service_placement": "per_project"}
    return [
        {**base, "name": DEFAULT, "default_verdict": "deny", "allow_hosts": general_hosts},
        {**base, "name": SCOPED, "default_verdict": "deny", "allow_hosts": []},
        {**base, "name": OPEN, "default_verdict": "allow", "allow_hosts": []},
        {**base, "name": OFFLINE, "default_verdict": "deny", "allow_hosts": [],
         "network_off": 1},
    ]


def legacy_profile_name(mode: str | None, inherit_general) -> str:
    """Old egress_policy mode -> the builtin that reproduces it."""
    if mode == "denyall":
        return OFFLINE
    if mode == "denylist":
        return OPEN
    if mode == "allowlist" and not inherit_general:
        return SCOPED
    return DEFAULT


def legacy_lists(mode: str | None, hosts: list[str]) -> tuple[list[str], list[str]]:
    """Old row hosts -> (project allow, project deny). denyall's hosts had no
    effect; they go to the deny list so a later move off Offline cannot turn
    them into allowed hosts."""
    if mode in ("denylist", "denyall"):
        return [], list(hosts)
    return list(hosts), []


async def _insert_profile(db, p: dict, builtin: int) -> int:
    cur = await db.execute(
        "INSERT INTO security_profiles(name, builtin, default_verdict, network_off, "
        "allow_hosts, deny_hosts, secrets, auto_handle, separate_box, box_image, "
        "box_mem_mb, box_runtime, allow_services, allow_package_requests, "
        "service_placement) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (p["name"], builtin, p["default_verdict"], int(p["network_off"]),
         json.dumps(p["allow_hosts"]), json.dumps(p["deny_hosts"]),
         json.dumps(p["secrets"]), int(p["auto_handle"]), int(p["separate_box"]),
         p["box_image"], p["box_mem_mb"], p["box_runtime"], int(p["allow_services"]),
         int(p["allow_package_requests"]), p["service_placement"]))
    return cur.lastrowid


async def _is_migrated(db) -> bool:
    async with db.execute("SELECT 1 FROM security_profiles WHERE builtin = 1 "
                          "AND name = ?", (DEFAULT,)) as cur:
        return await cur.fetchone() is not None


async def migrate(db: aiosqlite.Connection) -> dict | None:
    """The one-time move from per-project modes to profiles, in ONE transaction
    (BEGIN IMMEDIATE, or a savepoint inside a caller's open transaction), with
    the security event `profiles_migrated` written inside it. Returns
    the event detail, or None when there was nothing to do. Idempotent.

    Verdict equivalence (tests/test_profiles_migration.py proves it):
      __general__ hosts          -> Default.allow_hosts (deny-by-default)
      allowlist, inherit_general -> Default,  hosts -> project allow
      allowlist, no inherit      -> Scoped,   hosts -> project allow
      denylist                   -> Open,     hosts -> project deny
      denyall                    -> Offline,  hosts -> project deny (inert)
      no row                     -> Default,  empty project lists
    """
    if await _is_migrated(db):
        return None
    # Our own transaction takes the write lock UP FRONT (BEGIN IMMEDIATE): a
    # deferred one that read first could not upgrade while a racing
    # connection migrates ("database is locked"); this one waits its turn
    # (busy_timeout), then sees the builtins and does nothing. Inside a
    # caller's open transaction a savepoint keeps it atomic all the same.
    outer = db.in_transaction
    await db.execute("SAVEPOINT wp2_profiles" if outer else "BEGIN IMMEDIATE")

    async def _end(ok: bool) -> None:
        if outer:
            if not ok:
                await db.execute("ROLLBACK TO wp2_profiles")
            await db.execute("RELEASE wp2_profiles")
        elif ok:
            await db.commit()
        else:
            await db.rollback()

    try:
        if await _is_migrated(db):            # lost a race to another connection
            await _end(True)
            return None
        async with db.execute("SELECT project_slug, mode, inherit_general, hosts, "
                              "deny_hosts FROM egress_policy") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        general = next((r for r in rows if r["project_slug"] == GENERAL), None)
        gen_hosts = (json.loads(general["hosts"] or "[]") if general
                     else sorted(set(settings.egress_seed_hosts)))
        if general is None:
            # the row ensure_general would have seeded: kept for the audit trail
            await db.execute(
                "INSERT INTO egress_policy(project_slug, mode, inherit_general, hosts) "
                "VALUES (?, 'allowlist', 0, ?)", (GENERAL, json.dumps(gen_hosts)))
        ids: dict[str, int] = {}
        for p in _builtin_rows(gen_hosts):
            ids[p["name"]] = await _insert_profile(db, p, builtin=1)
        async with db.execute("SELECT slug FROM projects") as cur:
            project_slugs = {r["slug"] for r in await cur.fetchall()}
        moved, orphans = [], []
        for r in rows:
            slug = r["project_slug"]
            if slug == GENERAL:
                continue
            hosts = json.loads(r["hosts"] or "[]")
            extra_deny = json.loads(r.get("deny_hosts") or "[]")
            name = legacy_profile_name(r["mode"], r["inherit_general"])
            allow, deny = legacy_lists(r["mode"], hosts)
            deny = sorted(set(deny) | set(extra_deny))
            await db.execute(
                "UPDATE egress_policy SET hosts = ?, deny_hosts = ?, "
                "updated_at = datetime('now') WHERE project_slug = ?",
                (json.dumps(sorted(set(allow))), json.dumps(deny), slug))
            entry = {"slug": slug, "mode": r["mode"],
                     "inherit_general": int(r["inherit_general"] or 0),
                     "profile": name, "allow": len(allow), "deny": len(deny)}
            if slug in project_slugs:
                await db.execute("UPDATE projects SET profile_id = ? WHERE slug = ?",
                                 (ids[name], slug))
                moved.append(entry)
            else:
                # no projects row to point at a profile: its legacy `mode`
                # column is what resolves it (for_slug), so it keeps its verdicts
                orphans.append(entry)
        await db.execute("UPDATE projects SET profile_id = ? WHERE profile_id IS NULL",
                         (ids[DEFAULT],))
        from . import secrets as secrets_mod
        web_bound = sorted(n for n in secrets_mod.load() if secrets_mod.hosts_for(n))
        detail = {"profiles": ids, "default_allow_hosts": len(gen_hosts),
                  "projects": moved, "orphan_policies": orphans,
                  "projects_on_default": len(project_slugs) - len(moved),
                  # web_read secret use now needs a grant (the closed gap)
                  "web_bound_secrets_now_need_a_grant": web_bound}
        summary = (f"security profiles created ({len(ids)} builtins); "
                   f"{len(moved)} project policies moved, "
                   f"{len(project_slugs) - len(moved)} projects on Default")
        # pre-flagged for the operator: the reviewer never handles it (it is
        # on the never-list anyway) and it is not a queue item to triage
        cur = await db.execute(
            "INSERT INTO security_events(kind, severity, project_slug, summary, detail, "
            "triage_verdict, triage_reason, triage_at) "
            "VALUES ('profiles_migrated', 'info', NULL, ?, ?, 'flag', "
            "'one-time migration record: for the operator', datetime('now'))",
            (summary, json.dumps(detail)))
        event_id = cur.lastrowid
    except aiosqlite.IntegrityError:
        # only reachable inside a caller's savepoint (no write lock held):
        # another connection created the builtins first
        await _end(False)
        return None
    except BaseException:
        await _end(False)
        raise
    await _end(True)
    bus.publish(SECURITY_CHAN, {"type": "security_event", "id": event_id,
                                "kind": "profiles_migrated", "severity": "info",
                                "project": None, "summary": summary, "detail": detail})
    return detail


async def ensure_migrated(db: aiosqlite.Connection) -> None:
    """Cheap after the first call per database. Startup runs the migration
    (main.lifespan); this covers every other entry point (tests, scripts)."""
    key = str(settings.db_path)
    if key in _migrated:
        return
    # no lock: two racing callers are settled by the savepoint + the UNIQUE name
    await migrate(db)
    _migrated.add(key)


async def migrate_at_startup() -> None:
    """The main.lifespan call site (right after init_db)."""
    from .db import get_db
    db = await get_db()
    try:
        _migrated.discard(str(settings.db_path))
        await ensure_migrated(db)
    finally:
        await db.close()


# --- reads ------------------------------------------------------------------------

async def get(db: aiosqlite.Connection, profile_id: int) -> dict | None:
    await ensure_migrated(db)
    async with db.execute("SELECT * FROM security_profiles WHERE id = ?",
                          (profile_id,)) as cur:
        r = await cur.fetchone()
    return decode(r) if r else None


async def by_name(db: aiosqlite.Connection, name: str) -> dict | None:
    await ensure_migrated(db)
    async with db.execute("SELECT * FROM security_profiles WHERE name = ?",
                          (name,)) as cur:
        r = await cur.fetchone()
    return decode(r) if r else None


async def default(db: aiosqlite.Connection) -> dict:
    p = await by_name(db, DEFAULT)
    assert p is not None, "profiles migration did not create Default"
    return p


async def for_slug(db: aiosqlite.Connection, slug: str | None) -> dict:
    """The profile that governs `slug` (see the module docstring)."""
    await ensure_migrated(db)
    if not slug or slug == GENERAL:
        return await default(db)
    async with db.execute("SELECT profile_id FROM projects WHERE slug = ?",
                          (slug,)) as cur:
        pr = await cur.fetchone()
    if pr is not None:
        if pr["profile_id"] is not None:
            p = await get(db, pr["profile_id"])
            if p is not None:
                return p
        return await default(db)
    async with db.execute("SELECT mode, inherit_general FROM egress_policy "
                          "WHERE project_slug = ?", (slug,)) as cur:
        row = await cur.fetchone()
    if row is not None:
        p = await by_name(db, legacy_profile_name(row["mode"], row["inherit_general"]))
        if p is not None:
            return p
    return await default(db)


async def list_all(db: aiosqlite.Connection) -> list[dict]:
    await ensure_migrated(db)
    async with db.execute("SELECT * FROM security_profiles "
                          "ORDER BY builtin DESC, id") as cur:
        profs = [decode(r) for r in await cur.fetchall()]
    default_id = next((p["id"] for p in profs if p["name"] == DEFAULT and p["builtin"]), None)
    by_id = {p["id"]: p for p in profs}
    for p in profs:
        p["projects"] = []
    async with db.execute("SELECT slug, profile_id FROM projects WHERE "
                          "COALESCE(is_hidden, 0) = 0 AND deleted_at IS NULL "
                          "ORDER BY slug") as cur:
        for r in await cur.fetchall():
            pid = r["profile_id"] if r["profile_id"] in by_id else default_id
            if pid in by_id:
                by_id[pid]["projects"].append(r["slug"])
    return profs


def api_row(p: dict) -> dict:
    keys = ("id", "name", "builtin", "default_verdict", "network_off", "allow_hosts",
            "deny_hosts", "secrets", "auto_handle", "separate_box", "box_image",
            "box_mem_mb", "box_runtime", "allow_services", "allow_package_requests",
            "service_placement", "projects")
    return {k: p.get(k) for k in keys if k in p}


# --- writes (operator only; every change is a `profile_changed` event) -------------

def diff(old: dict, new: dict) -> dict:
    """{field: {"from": a, "to": b}} for every profile field that changed."""
    out = {}
    for f in FIELDS:
        if f in new and old.get(f) != new.get(f):
            out[f] = {"from": old.get(f), "to": new.get(f)}
    return out


def validate(body: dict, *, current: dict | None = None) -> dict:
    """A full, normalized profile from a create/edit body. `service_placement`
    and `box_runtime` must be in the body itself on create AND edit (operator
    decision 0.1: never inferred, never defaulted)."""
    for req in ("service_placement", "box_runtime"):
        if body.get(req) in (None, ""):
            raise ProfileError(
                f"{req} is required (no default)" + (
                    "; PUT takes the full row, the same body as POST"
                    if current is not None else ""), status=422)
    base = dict(current or {})
    p = {**{"default_verdict": "deny", "network_off": False, "allow_hosts": [],
            "deny_hosts": [], "secrets": [], "auto_handle": False,
            "separate_box": False, "box_image": "main", "box_mem_mb": None,
            "allow_services": False, "allow_package_requests": False}, **base}
    for f in FIELDS:
        if f in body and body[f] is not None:
            p[f] = body[f]
        elif f == "box_mem_mb" and "box_mem_mb" in body:
            p[f] = None
    name = str(p.get("name") or "").strip()
    if not name or len(name) > 64:
        raise ProfileError("name is required (at most 64 characters)")
    p["name"] = name
    if p["default_verdict"] not in VERDICTS:
        raise ProfileError("default_verdict must be deny|allow")
    if p["service_placement"] not in PLACEMENTS:
        raise ProfileError("service_placement must be per_service|per_project|shared",
                           status=422)
    if p["box_runtime"] not in RUNTIMES:
        raise ProfileError("box_runtime must be kvm|docker", status=422)
    if not _VARIANT_RE.match(str(p["box_image"] or "")):
        raise ProfileError("box_image must be an image variant name")
    if p["box_mem_mb"] is not None:
        try:
            p["box_mem_mb"] = int(p["box_mem_mb"])
        except (TypeError, ValueError):
            raise ProfileError("box_mem_mb must be an integer or null") from None
        if not 256 <= p["box_mem_mb"] <= 16384:
            raise ProfileError("box_mem_mb must be between 256 and 16384")
    p["allow_hosts"] = norm_hosts(p["allow_hosts"])
    p["deny_hosts"] = norm_hosts(p["deny_hosts"])
    p["secrets"] = norm_secrets(p["secrets"])
    for f in _BOOL_FIELDS:
        p[f] = bool(p[f])
    return p


async def _raise_changed(db, summary: str, detail: dict, project: str | None = None,
                         severity: str = "warn") -> None:
    from . import security
    await security.raise_event(db, kind="profile_changed", severity=severity,
                               project=project, summary=summary, detail=detail)


def _severity(changes: dict) -> str:
    """Widening the default or turning the network on is the one-click
    high-impact change the threat model names: make it critical."""
    dv = changes.get("default_verdict", {})
    no = changes.get("network_off", {})
    if dv.get("to") == "allow" or no.get("to") is False:
        return "critical"
    return "warn"


async def create(db: aiosqlite.Connection, body: dict, *, actor: str = "operator") -> dict:
    await ensure_migrated(db)
    p = validate(body)
    if await by_name(db, p["name"]):
        raise ProfileError(f"a profile named {p['name']!r} already exists", status=409)
    pid = await _insert_profile(db, p, builtin=0)
    await db.commit()
    new = await get(db, pid)
    await _raise_changed(db, f"profile {new['name']!r} created by {actor}",
                         {"profile": {"id": pid, "name": new["name"]}, "action": "create",
                          "changes": diff({}, new)},
                         severity=_severity(diff({}, new)))
    return new


async def update(db: aiosqlite.Connection, profile_id: int, body: dict, *,
                 actor: str = "operator") -> dict:
    cur_p = await get(db, profile_id)
    if cur_p is None:
        raise ProfileError("no such profile", status=404)
    if cur_p["builtin"] and body.get("name") not in (None, cur_p["name"]):
        raise ProfileError("a builtin profile cannot be renamed", status=409)
    p = validate(body, current=cur_p)
    if p["name"] != cur_p["name"] and await by_name(db, p["name"]):
        raise ProfileError(f"a profile named {p['name']!r} already exists", status=409)
    changes = diff(cur_p, p)
    if not changes:
        return cur_p
    await db.execute(
        "UPDATE security_profiles SET name=?, default_verdict=?, network_off=?, "
        "allow_hosts=?, deny_hosts=?, secrets=?, auto_handle=?, separate_box=?, "
        "box_image=?, box_mem_mb=?, box_runtime=?, allow_services=?, "
        "allow_package_requests=?, service_placement=?, updated_at=datetime('now') "
        "WHERE id = ?",
        (p["name"], p["default_verdict"], int(p["network_off"]),
         json.dumps(p["allow_hosts"]), json.dumps(p["deny_hosts"]),
         json.dumps(p["secrets"]), int(p["auto_handle"]), int(p["separate_box"]),
         p["box_image"], p["box_mem_mb"], p["box_runtime"], int(p["allow_services"]),
         int(p["allow_package_requests"]), p["service_placement"], profile_id))
    await db.commit()
    await _raise_changed(db, f"profile {p['name']!r} changed by {actor}: "
                             + ", ".join(sorted(changes)),
                         {"profile": {"id": profile_id, "name": p["name"]},
                          "action": "update", "changes": changes},
                         severity=_severity(changes))
    return await get(db, profile_id)


async def set_hosts(db: aiosqlite.Connection, profile_id: int, *, allow=None,
                    deny=None, actor: str = "operator") -> dict:
    """Replace a profile's allow and/or deny list (the Network page's revoke
    and "promote to profile"). Same event as any other profile edit."""
    cur_p = await get(db, profile_id)
    if cur_p is None:
        raise ProfileError("no such profile", status=404)
    body = {"service_placement": cur_p["service_placement"],
            "box_runtime": cur_p["box_runtime"]}
    if allow is not None:
        body["allow_hosts"] = allow
    if deny is not None:
        body["deny_hosts"] = deny
    return await update(db, profile_id, body, actor=actor)


async def delete(db: aiosqlite.Connection, profile_id: int, *,
                 actor: str = "operator") -> dict:
    p = await get(db, profile_id)
    if p is None:
        raise ProfileError("no such profile", status=404)
    if p["builtin"]:
        raise ProfileError("builtin profiles cannot be deleted", status=409)
    async with db.execute("SELECT slug FROM projects WHERE profile_id = ?",
                          (profile_id,)) as cur:
        users = [r["slug"] for r in await cur.fetchall()]
    if users:
        raise ProfileError("profile is in use by: " + ", ".join(users[:20])
                           + " (move them to another profile first)", status=409)
    await db.execute("DELETE FROM security_profiles WHERE id = ?", (profile_id,))
    await db.commit()
    await _raise_changed(db, f"profile {p['name']!r} deleted by {actor}",
                         {"profile": {"id": profile_id, "name": p["name"]},
                          "action": "delete", "changes": diff(p, {})})
    return {"ok": True, "id": profile_id}


async def assign(db: aiosqlite.Connection, slug: str, profile_id: int, *,
                 actor: str = "operator", require_project: bool = True) -> dict:
    """Point a project at a profile. `require_project=False` (the legacy
    set_policy path only) lets a slug with no projects row be assigned a
    BUILTIN through its egress_policy row's legacy mode instead."""
    if slug == "__image_build__":
        # builder boxes' fixed registry-only policy (egress.IMAGE_BUILD)
        raise ProfileError("the image-build policy is fixed", status=409)
    new = await get(db, profile_id)
    if new is None:
        raise ProfileError("no such profile", status=404)
    old = await for_slug(db, slug)
    async with db.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)) as cur:
        is_project = await cur.fetchone() is not None
    if is_project:
        await db.execute("UPDATE projects SET profile_id = ? WHERE slug = ?",
                         (profile_id, slug))
    elif require_project:
        raise ProfileError("no such project", status=404)
    elif not new["builtin"]:
        raise ProfileError("only a builtin profile can govern a non-project slug")
    # keep the legacy mode column in step: it is what resolves a non-project slug
    mode, inherit = {OFFLINE: ("denyall", 0), OPEN: ("denylist", 0),
                     SCOPED: ("allowlist", 0)}.get(new["name"], ("allowlist", 1))
    if new["builtin"]:
        await db.execute(
            "INSERT INTO egress_policy(project_slug, mode, inherit_general) VALUES (?,?,?) "
            "ON CONFLICT(project_slug) DO UPDATE SET mode = excluded.mode, "
            "inherit_general = excluded.inherit_general", (slug, mode, inherit))
    await db.commit()
    if old["id"] != new["id"]:
        changes = diff(old, new)
        changes.pop("name", None)
        await _raise_changed(
            db, f"project {slug} moved from profile {old['name']!r} to "
                f"{new['name']!r} by {actor}",
            {"project": slug, "action": "assign",
             "from": {"id": old["id"], "name": old["name"]},
             "to": {"id": new["id"], "name": new["name"]}, "changes": changes},
            project=slug, severity=_severity(changes))
    return {"ok": True, "project": slug,
            "profile": {"id": new["id"], "name": new["name"]}}
