"""Security profiles (DESIGN-BOXES.md (d); the WP2 half of docs/boxes-contract.md).

A profile is the shared baseline a project runs under: its egress default and
lists, the secrets every project under it may use, whether the triage reviewer
may handle its items, and how its boxes are built. A project points at one via
`projects.profile_id` (NULL = the profile marked `is_default`). The project's
OWN allow/deny lists stay in `egress_policy` (`hosts` = allow, `deny_hosts` =
deny) and are the only thing approvals ever train (egress.py).

No profile is seeded on a fresh install: first-run setup (backend.cli setup,
the web /setup page) creates the one new projects use and marks it the
default. If setup was skipped, the first use creates the same safe default
(ask for new sites, shared box, no services, no packages). Every profile can
be renamed, edited and deleted, except one a project still points at and the
marked default (make another the default first). Exactly one row is the
default whenever any row exists (a partial unique index keeps it at most one;
this module keeps it at least one).

An install that predates profiles is migrated once from its per-project
modes: `Default` (the old `__general__` list, deny-by-default, marked the
default), plus `Scoped` / `Open` / `Offline` only where a project used the
matching old mode. Installs migrated by the old code keep their four rows;
the old `builtin` flag is cleared and the row named Default is marked.

Profile resolution for a slug (for_slug):
  * None / `__general__` (unattributed shared-box traffic) -> the default.
  * a projects row -> its profile_id, NULL -> the default.
  * no projects row but an egress_policy row (a policy set for a slug that is
    not a project: tests, a hard-deleted project) -> the profile its row's
    legacy `mode` maps to (by name; the default for allowlist+inherit).
  * anything else -> the default.
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
LEGACY_NAMES = (DEFAULT, SCOPED, OPEN, OFFLINE)   # the pre-profiles modes' shapes

# first-run setup's choices (setup_fields)
NETWORK_CHOICES = ("ask", "allow", "off")
PLACEMENT_CHOICES = ("shared", "vm", "container")

# the fields a profile carries (the API row shape, minus id/is_default/projects)
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
    for f in _BOOL_FIELDS + ("builtin", "is_default"):
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


# --- transactions ----------------------------------------------------------------

class _Txn:
    """BEGIN IMMEDIATE (the write lock up front: a deferred transaction that
    read first could not upgrade while a racing connection writes), or a
    savepoint inside a caller's open transaction. Commits on success, rolls
    back on any exception (which propagates)."""

    def __init__(self, db, name: str = "profiles_txn"):
        self.db, self.name = db, name

    async def __aenter__(self):
        self.outer = self.db.in_transaction
        await self.db.execute(f"SAVEPOINT {self.name}" if self.outer else "BEGIN IMMEDIATE")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.outer:
            if exc_type is not None:
                await self.db.execute(f"ROLLBACK TO {self.name}")
            await self.db.execute(f"RELEASE {self.name}")
        elif exc_type is None:
            await self.db.commit()
        else:
            await self.db.rollback()
        return False


# --- shapes -----------------------------------------------------------------------

def setup_fields(network: str = "ask", placement: str = "shared",
                 services: bool = False, packages: bool = False) -> dict:
    """First-run setup's four answers -> the profile fields they set.
      network   ask   = deny-by-default, new sites queue for approval
                allow = allow-by-default
                off   = no network at all
      placement shared = the shared box; vm = the project's own KVM box;
                container = its own hardened Docker container
      services / packages = allow_services / allow_package_requests"""
    if network not in NETWORK_CHOICES:
        raise ProfileError("network must be ask|allow|off")
    if placement not in PLACEMENT_CHOICES:
        raise ProfileError("placement must be shared|vm|container")
    return {"default_verdict": "allow" if network == "allow" else "deny",
            "network_off": network == "off",
            "separate_box": placement != "shared",
            "box_runtime": "docker" if placement == "container" else "kvm",
            "allow_services": bool(services),
            "allow_package_requests": bool(packages),
            "service_placement": "per_project"}


def setup_choices(p: dict) -> dict:
    """The reverse of setup_fields: what setup would show for profile `p`."""
    network = ("off" if p.get("network_off") else
               "allow" if p.get("default_verdict") == "allow" else "ask")
    placement = ("shared" if not p.get("separate_box") else
                 "container" if p.get("box_runtime") == "docker" else "vm")
    return {"network": network, "placement": placement,
            "services": bool(p.get("allow_services")),
            "packages": bool(p.get("allow_package_requests"))}


_NETWORK_WORDS = {"ask": "ask me about new sites", "allow": "allow new sites",
                  "off": "no network"}
_PLACEMENT_WORDS = {"shared": "the shared box", "vm": "its own VM",
                    "container": "its own container"}


def describe(p: dict) -> str:
    c = setup_choices(p)
    return (f"{p.get('name')}: network {_NETWORK_WORDS[c['network']]} · runs in "
            f"{_PLACEMENT_WORDS[c['placement']]} · services "
            f"{'yes' if c['services'] else 'no'} · packages "
            f"{'yes' if c['packages'] else 'no'}")


def default_body(name: str = DEFAULT, **choices) -> dict:
    """A full profile body for the default setup creates (and the one the
    first use creates when setup was skipped: every choice at its safe
    value). The seed hosts are its allow list, the old shared baseline."""
    return {"name": name, "allow_hosts": sorted(set(settings.egress_seed_hosts)),
            "deny_hosts": [], "secrets": [], "auto_handle": True,
            "box_image": "main", "box_mem_mb": None, **setup_fields(**choices)}


def _legacy_rows(general_hosts: list[str]) -> dict[str, dict]:
    """The shapes the old per-project modes map to. `service_placement` and
    `box_runtime` are set EXPLICITLY (operator decision 0.1: never a default).
    auto_handle is ON so the reviewer keeps handling exactly what it handled
    before profiles; the global reviewer switch stays the master kill."""
    base = {"network_off": 0, "deny_hosts": [], "secrets": [], "auto_handle": 1,
            "separate_box": 0, "box_image": "main", "box_mem_mb": None,
            "box_runtime": "kvm", "allow_services": 0, "allow_package_requests": 0,
            "service_placement": "per_project"}
    return {
        DEFAULT: {**base, "name": DEFAULT, "default_verdict": "deny",
                  "allow_hosts": general_hosts},
        SCOPED: {**base, "name": SCOPED, "default_verdict": "deny", "allow_hosts": []},
        OPEN: {**base, "name": OPEN, "default_verdict": "allow", "allow_hosts": []},
        OFFLINE: {**base, "name": OFFLINE, "default_verdict": "deny", "allow_hosts": [],
                  "network_off": 1},
    }


def legacy_profile_name(mode: str | None, inherit_general) -> str:
    """Old egress_policy mode -> the legacy profile that reproduces it."""
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


async def _insert_profile(db, p: dict, is_default: int = 0) -> int:
    cur = await db.execute(
        "INSERT INTO security_profiles(name, builtin, is_default, default_verdict, "
        "network_off, allow_hosts, deny_hosts, secrets, auto_handle, separate_box, "
        "box_image, box_mem_mb, box_runtime, allow_services, allow_package_requests, "
        "service_placement) VALUES (?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (p["name"], int(is_default), p["default_verdict"], int(p["network_off"]),
         json.dumps(p["allow_hosts"]), json.dumps(p["deny_hosts"]),
         json.dumps(p["secrets"]), int(p["auto_handle"]), int(p["separate_box"]),
         p["box_image"], p["box_mem_mb"], p["box_runtime"], int(p["allow_services"]),
         int(p["allow_package_requests"]), p["service_placement"]))
    return cur.lastrowid


# --- migration -------------------------------------------------------------------

_migrated: set[str] = set()          # db paths known to be migrated (this process)


async def _one(db, sql: str, args=()) -> object:
    async with db.execute(sql, args) as cur:
        return await cur.fetchone()


async def _needs_normalizing(db) -> bool:
    """Rows exist but none is marked, or an old builtin flag is still set."""
    if await _one(db, "SELECT 1 FROM security_profiles WHERE builtin = 1 LIMIT 1"):
        return True
    return (await _one(db, "SELECT 1 FROM security_profiles LIMIT 1") is not None
            and await _one(db, "SELECT 1 FROM security_profiles WHERE is_default = 1")
            is None)


async def _mark_some_default(db) -> None:
    """Inside a transaction: when rows exist and none is marked, mark the one
    acting as default today (the old builtin Default, else a row named
    Default, else the oldest)."""
    if await _one(db, "SELECT 1 FROM security_profiles WHERE is_default = 1"):
        return
    r = (await _one(db, "SELECT id FROM security_profiles WHERE builtin = 1 AND name = ?",
                    (DEFAULT,))
         or await _one(db, "SELECT id FROM security_profiles WHERE name = ?", (DEFAULT,))
         or await _one(db, "SELECT id FROM security_profiles ORDER BY id LIMIT 1"))
    if r is not None:
        await db.execute("UPDATE security_profiles SET is_default = 1 WHERE id = ?",
                         (r["id"],))


async def _normalize(db) -> None:
    """Installs migrated by the old code: mark the row acting as default and
    clear the builtin flag. Nothing is deleted or reassigned."""
    try:
        async with _Txn(db, "profiles_norm"):
            await _mark_some_default(db)
            await db.execute("UPDATE security_profiles SET builtin = 0 WHERE builtin = 1")
    except aiosqlite.IntegrityError:
        pass                          # a racing connection marked one first


async def _legacy_data(db) -> bool:
    """An install from before profiles: it has egress_policy rows (the old
    shared `__general__` list, per-project modes). This code never writes
    one before a profile exists (every egress decision resolves the default
    first), so a fresh install has none."""
    return await _one(db, "SELECT 1 FROM egress_policy LIMIT 1") is not None


async def migrate(db: aiosqlite.Connection) -> dict | None:
    """The one-time move from per-project modes to profiles, in ONE transaction,
    with the security event `profiles_migrated` written inside it. Returns the
    event detail, or None when there was nothing to do. Idempotent. A fresh
    install (no egress_policy rows) gets NO profile here: setup,
    or the first use (default()), creates the one it needs.

    Verdict equivalence (tests/test_profiles_migration.py proves it):
      __general__ hosts          -> Default.allow_hosts (deny-by-default)
      allowlist, inherit_general -> Default,  hosts -> project allow
      allowlist, no inherit      -> Scoped,   hosts -> project allow
      denylist                   -> Open,     hosts -> project deny
      denyall                    -> Offline,  hosts -> project deny (inert)
      no row                     -> Default,  empty project lists
    Only Default and the legacy profiles some project actually used are made.
    """
    if await _one(db, "SELECT 1 FROM security_profiles LIMIT 1"):
        if await _needs_normalizing(db):
            await _normalize(db)
        return None
    if not await _legacy_data(db):
        return None
    try:
        async with _Txn(db, "wp2_profiles"):
            if await _one(db, "SELECT 1 FROM security_profiles LIMIT 1"):
                return None                   # lost a race to another connection
            detail, summary, event_id = await _migrate_rows(db)
    except aiosqlite.IntegrityError:
        # only reachable inside a caller's savepoint (no write lock held):
        # another connection created the profiles first
        return None
    bus.publish(SECURITY_CHAN, {"type": "security_event", "id": event_id,
                                "kind": "profiles_migrated", "severity": "info",
                                "project": None, "summary": summary, "detail": detail})
    return detail


async def _migrate_rows(db) -> tuple[dict, str, int]:
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
    shapes = _legacy_rows(gen_hosts)
    ids: dict[str, int] = {DEFAULT: await _insert_profile(db, shapes[DEFAULT], 1)}

    async def _id(name: str) -> int:
        if name not in ids:
            ids[name] = await _insert_profile(db, shapes[name])
        return ids[name]

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
        pid = await _id(name)
        if slug in project_slugs:
            await db.execute("UPDATE projects SET profile_id = ? WHERE slug = ?",
                             (pid, slug))
            moved.append(entry)
        else:
            # no projects row to point at a profile: its legacy `mode`
            # column is what resolves it (for_slug), so it keeps its verdicts
            orphans.append(entry)
    await db.execute("UPDATE projects SET profile_id = ? WHERE profile_id IS NULL",
                     (ids[DEFAULT],))
    from . import secrets as secrets_mod
    web_bound = sorted(n for n in secrets_mod.load() if secrets_mod.hosts_for(n))
    detail = {"profiles": ids, "default": ids[DEFAULT],
              "default_allow_hosts": len(gen_hosts),
              "projects": moved, "orphan_policies": orphans,
              "projects_on_default": len(project_slugs) - len(moved),
              # web_read secret use now needs a grant (the closed gap)
              "web_bound_secrets_now_need_a_grant": web_bound}
    summary = (f"security profiles created ({len(ids)}); "
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
    return detail, summary, cur.lastrowid


async def ensure_migrated(db: aiosqlite.Connection) -> None:
    """Cheap after the first call per database. Startup runs the migration
    (main.lifespan); this covers every other entry point (tests, scripts)."""
    key = str(settings.db_path)
    if key in _migrated:
        return
    # no lock: two racing callers are settled by the transaction + UNIQUE name
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
    r = await _one(db, "SELECT * FROM security_profiles WHERE id = ?", (profile_id,))
    return decode(r) if r else None


async def by_name(db: aiosqlite.Connection, name: str) -> dict | None:
    await ensure_migrated(db)
    r = await _one(db, "SELECT * FROM security_profiles WHERE name = ?", (name,))
    return decode(r) if r else None


async def current_default(db: aiosqlite.Connection) -> dict | None:
    """The marked default, or None when no profile exists yet. Never creates
    one (setup uses this to decide whether its step has anything to do)."""
    await ensure_migrated(db)
    r = await _one(db, "SELECT * FROM security_profiles WHERE is_default = 1")
    return decode(r) if r else None


async def default(db: aiosqlite.Connection) -> dict:
    """The profile new and unassigned projects use. The first use on an
    install that has none (setup skipped) creates the safe default."""
    p = await current_default(db)
    if p is not None:
        return p
    created = False
    try:
        async with _Txn(db, "profiles_default"):
            if await _one(db, "SELECT 1 FROM security_profiles LIMIT 1"):
                await _mark_some_default(db)
            else:
                await _insert_profile(db, validate(default_body()), is_default=1)
                created = True
    except aiosqlite.IntegrityError:
        created = False               # a racing connection made or marked one
    p = await current_default(db)
    assert p is not None, "no default security profile"
    if created:
        # pre-flagged like the migration record: for the operator, not a
        # queue item for the reviewer
        summary = (f"default profile {p['name']!r} created on first use "
                   "(setup did not configure one)")
        detail = {"profile": {"id": p["id"], "name": p["name"]},
                  "action": "create_default", "changes": diff({}, p)}
        cur = await db.execute(
            "INSERT INTO security_events(kind, severity, project_slug, summary, detail, "
            "triage_verdict, triage_reason, triage_at) "
            "VALUES ('profile_changed', 'info', NULL, ?, ?, 'flag', "
            "'automatic default profile: for the operator', datetime('now'))",
            (summary, json.dumps(detail)))
        await db.commit()
        bus.publish(SECURITY_CHAN, {"type": "security_event", "id": cur.lastrowid,
                                    "kind": "profile_changed", "severity": "info",
                                    "project": None, "summary": summary,
                                    "detail": detail})
    return p


async def legacy_profile(db: aiosqlite.Connection, name: str) -> dict:
    """The profile an old mode maps to (egress.set_policy, the pre-profiles
    call): the default for allowlist+inherit, else the profile of that name,
    created in its legacy shape on first need."""
    dflt = await default(db)          # first, so a legacy shape never becomes it
    if name == DEFAULT:
        return dflt
    p = await by_name(db, name)
    if p is not None:
        return p
    shape = _legacy_rows(sorted(set(settings.egress_seed_hosts)))[name]
    try:
        return await create(db, shape, actor="legacy policy call")
    except ProfileError:
        return await by_name(db, name)      # created by a racing call


async def for_slug(db: aiosqlite.Connection, slug: str | None) -> dict:
    """The profile that governs `slug` (see the module docstring)."""
    await ensure_migrated(db)
    if not slug or slug == GENERAL:
        return await default(db)
    pr = await _one(db, "SELECT profile_id FROM projects WHERE slug = ?", (slug,))
    if pr is not None:
        if pr["profile_id"] is not None:
            p = await get(db, pr["profile_id"])
            if p is not None:
                return p
        return await default(db)
    row = await _one(db, "SELECT mode, inherit_general FROM egress_policy "
                         "WHERE project_slug = ?", (slug,))
    if row is not None:
        name = legacy_profile_name(row["mode"], row["inherit_general"])
        if name != DEFAULT:
            p = await by_name(db, name)
            if p is not None:
                return p
    return await default(db)


async def list_all(db: aiosqlite.Connection) -> list[dict]:
    """Every profile, the default first, each with the visible projects it
    governs (`projects`; NULL profile_id counts under the default)."""
    default_id = (await default(db))["id"]
    async with db.execute("SELECT * FROM security_profiles "
                          "ORDER BY is_default DESC, id") as cur:
        profs = [decode(r) for r in await cur.fetchall()]
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
    keys = ("id", "name", "is_default", "default_verdict", "network_off", "allow_hosts",
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
    """A full, normalized profile from a create/edit body. On create, a body
    without `service_placement` / `box_runtime` gets the defaults a new
    profile has in the form: per_project services on the shared box (runtime
    kvm, which only matters once `separate_box` is set). An edit (PUT takes
    the full row, the same body as POST) must still name both."""
    if current is not None:
        for req in ("service_placement", "box_runtime"):
            if body.get(req) in (None, ""):
                raise ProfileError(
                    f"{req} is required; PUT takes the full row, the same body as POST",
                    status=422)
    base = dict(current or {})
    p = {**{"default_verdict": "deny", "network_off": False, "allow_hosts": [],
            "deny_hosts": [], "secrets": [], "auto_handle": False,
            "separate_box": False, "box_image": "main", "box_mem_mb": None,
            "allow_services": False, "allow_package_requests": False,
            "service_placement": "per_project", "box_runtime": "kvm"}, **base}
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


async def create(db: aiosqlite.Connection, body: dict, *, actor: str = "operator",
                 make_default: bool = False) -> dict:
    """A new profile; the default only when asked to (`make_default`: setup).
    On an install with no profile yet, the safe default is created first, so
    a profile made for one project never silently becomes everyone's."""
    await ensure_migrated(db)
    p = validate(body)
    if not make_default:
        await default(db)
    if await by_name(db, p["name"]):
        raise ProfileError(f"a profile named {p['name']!r} already exists", status=409)
    try:
        async with _Txn(db, "profiles_create"):
            first = await _one(db, "SELECT 1 FROM security_profiles "
                                   "WHERE is_default = 1") is None
            if make_default and not first:
                await db.execute("UPDATE security_profiles SET is_default = 0 "
                                 "WHERE is_default = 1")
            pid = await _insert_profile(db, p, is_default=int(make_default or first))
    except aiosqlite.IntegrityError:
        raise ProfileError(f"a profile named {p['name']!r} already exists",
                           status=409) from None
    new = await get(db, pid)
    what = "created as the default" if new["is_default"] else "created"
    await _raise_changed(db, f"profile {new['name']!r} {what} by {actor}",
                         {"profile": {"id": pid, "name": new["name"]}, "action": "create",
                          "is_default": new["is_default"], "changes": diff({}, new)},
                         severity=_severity(diff({}, new)))
    return new


async def _following_default(db) -> list[str]:
    async with db.execute("SELECT slug FROM projects WHERE profile_id IS NULL "
                          "AND deleted_at IS NULL ORDER BY slug") as cur:
        return [r["slug"] for r in await cur.fetchall()]


async def set_default(db: aiosqlite.Connection, profile_id: int, *,
                      actor: str = "operator") -> dict:
    """Mark `profile_id` as the default (the one new and unassigned projects
    use). Projects with no profile of their own move with it: the event names
    them and diffs the two profiles."""
    new = await get(db, profile_id)
    if new is None:
        raise ProfileError("no such profile", status=404)
    if new["is_default"]:
        return new
    old = await current_default(db)
    async with _Txn(db, "profiles_mark"):
        await db.execute("UPDATE security_profiles SET is_default = 0 WHERE is_default = 1")
        await db.execute("UPDATE security_profiles SET is_default = 1 WHERE id = ?",
                         (profile_id,))
    new = await get(db, profile_id)
    changes = diff(old or {}, new)
    changes.pop("name", None)
    following = await _following_default(db)
    await _raise_changed(
        db, f"profile {new['name']!r} made the default by {actor}"
            + (f" (was {old['name']!r})" if old else "")
            + (f"; {len(following)} project(s) without their own profile move with it"
               if following else ""),
        {"profile": {"id": profile_id, "name": new["name"]}, "action": "make_default",
         "from": {"id": old["id"], "name": old["name"]} if old else None,
         "projects": following, "changes": changes},
        severity=_severity(changes) if following else "warn")
    return new


async def configure_default(db: aiosqlite.Connection, *, actor: str = "setup",
                            **choices) -> dict:
    """First-run setup's profile step: create the default from the four
    answers, or (asked to change it) set those answers on the existing
    default, keeping its name, lists and secrets."""
    fields = setup_fields(**choices)
    cur = await current_default(db)
    if cur is None:
        return await create(db, default_body(**choices), actor=actor, make_default=True)
    return await update(db, cur["id"], fields, actor=actor)


async def update(db: aiosqlite.Connection, profile_id: int, body: dict, *,
                 actor: str = "operator") -> dict:
    cur_p = await get(db, profile_id)
    if cur_p is None:
        raise ProfileError("no such profile", status=404)
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
    if p["is_default"]:
        raise ProfileError(f"{p['name']!r} is the default profile for new projects: "
                           "make another profile the default first", status=409)
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
    set_policy path only) lets a slug with no projects row be governed through
    its egress_policy row's legacy mode instead: only the default or a profile
    named like a legacy mode (Scoped / Open / Offline) can be reached that way."""
    if slug == "__image_build__":
        # builder boxes' fixed registry-only policy (egress.IMAGE_BUILD)
        raise ProfileError("the image-build policy is fixed", status=409)
    new = await get(db, profile_id)
    if new is None:
        raise ProfileError("no such profile", status=404)
    old = await for_slug(db, slug)
    legacy = ({OFFLINE: ("denyall", 0), OPEN: ("denylist", 0),
               SCOPED: ("allowlist", 0)}.get(new["name"])
              or (("allowlist", 1) if new["is_default"] else None))
    async with db.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)) as cur:
        is_project = await cur.fetchone() is not None
    if is_project:
        await db.execute("UPDATE projects SET profile_id = ? WHERE slug = ?",
                         (profile_id, slug))
    elif require_project:
        raise ProfileError("no such project", status=404)
    elif legacy is None:
        raise ProfileError("only the default or a legacy-mode profile can govern "
                           "a non-project slug")
    # keep the legacy mode column in step: it is what resolves a non-project slug
    if legacy is not None:
        await db.execute(
            "INSERT INTO egress_policy(project_slug, mode, inherit_general) VALUES (?,?,?) "
            "ON CONFLICT(project_slug) DO UPDATE SET mode = excluded.mode, "
            "inherit_general = excluded.inherit_general", (slug, *legacy))
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
