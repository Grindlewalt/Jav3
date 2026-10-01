"""Persistent package requests: the catalogue (DESIGN-BOXES 2(f), WP5).

An agent asks for a package with the `package_request` tool; the operator
approves or rejects it; an approved package is added to the recipe of the image
variant the requesting project's profile uses, and a NEW version of that
variant is built (a running image is never touched). Every project on that
variant gets the package: that is what a variant is (operator decision 0.2),
so the approval card lists them.

Three rules carry the security weight:

1. The package NAME and VERSION are validated per manager against a strict
   grammar. URLs, paths, `git+`, index/registry flags, option-looking tokens
   and shell metacharacters are refused before anything is stored.
2. The host builds the CANONICAL command from the validated fields. The
   agent's `install_command` is stored for the operator to read and is never
   executed, parsed for arguments or passed anywhere.
3. Nothing is auto-handled. Approval needs a cookie-authed operator, an
   explicit acknowledge, and a dry-run resolution (version + integrity) that
   the operator saw on the card.

Status machine (`TRANSITIONS`):

    pending -> approved | rejected
    approved -> building -> built | failed
    failed -> building                  (a retry build)
    built | approved | failed -> removed (the next build drops it)
"""
from __future__ import annotations

import re
import shlex

from . import security

MANAGERS = ("apt", "pip", "npm")
STATUSES = ("pending", "approved", "rejected", "building", "built", "failed",
            "removed")
TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"approved", "rejected"}),
    "approved": frozenset({"building", "removed"}),
    "building": frozenset({"built", "failed"}),
    "built": frozenset({"removed", "building"}),
    "failed": frozenset({"building", "removed"}),
    "rejected": frozenset(),
    "removed": frozenset(),
}

# where pip installs go inside an image (a venv first on PATH: no PEP 668
# fight with Debian's python) and where npm -g installs go
PIP_BIN = "/opt/jav3/py/bin/pip"
NPM_PREFIX = "/usr/local"

REASON_MAX = 1000
COMMAND_MAX = 500


class PackageError(ValueError):
    """A request that is refused. The message is safe to show the agent."""


# --- validation -------------------------------------------------------------

# Anything in here is refused outright, whatever the manager: shell syntax,
# quoting, whitespace, and the characters a URL/path/flag needs.
_FORBIDDEN = set(";|&$`<>(){}[]*?!'\"\\#%,^\n\r\t ")

_NAME_RE = {
    # Debian policy: lower-case alnum and + - . ; at least two chars; alnum first
    "apt": re.compile(r"^[a-z0-9][a-z0-9+.-]{1,62}$"),
    # PEP 508 name (no extras, no markers): alnum at both ends
    "pip": re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$"),
    # npm: optional @scope/, lower-case, no leading . or _
    "npm": re.compile(r"^(?:@[a-z0-9][a-z0-9._-]{0,99}/)?[a-z0-9][a-z0-9._-]{0,113}$"),
}

_VERSION_RE = {
    # Debian version: optional epoch, upstream, optional revision
    "apt": re.compile(r"^(?:[0-9]{1,4}:)?[0-9][A-Za-z0-9.+~]{0,62}(?:-[A-Za-z0-9.+~]{1,30})?$"),
    # PEP 440 public version (exact pin only; no operators, no local label)
    "pip": re.compile(r"^(?:[0-9]+!)?[0-9]+(?:\.[0-9]+){0,5}"
                      r"(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?$"),
    # semver, exact
    "npm": re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]{1,40})?"
                      r"(?:\+[0-9A-Za-z.-]{1,40})?$"),
}

# in the agent's free-text command: signs it wants a non-default source
_SOURCE_FLAG_RE = re.compile(
    r"(?:--?(?:index-url|extra-index-url|i|f|find-links|trusted-host|registry|"
    r"userconfig|globalconfig|target|prefix|root|src|editable|e|r|requirement|"
    r"constraint|c|allow-unauthenticated|option|o|config)\b)"
    r"|(?:\b[a-z][a-z0-9+.-]*://)|(?:\bgit\+)|(?:\bfile:)|(?:\blink:)|(?:\bnpm:)",
    re.IGNORECASE)


def validate_name(manager: str, package: str) -> str:
    """The package name, normalised, or PackageError. Pure."""
    if manager not in MANAGERS:
        raise PackageError(f"manager must be one of {', '.join(MANAGERS)}")
    if not isinstance(package, str) or not package:
        raise PackageError("package is required")
    p = package.strip()
    if p != package or not p:
        raise PackageError("package must not contain whitespace")
    if set(p) & _FORBIDDEN:
        raise PackageError("package contains characters a package name never has "
                           "(shell syntax, quotes, spaces or brackets)")
    low = p.lower()
    if "://" in p or low.startswith(("git+", "git:", "file:", "http:", "https:",
                                     "link:", "npm:", "github:")):
        raise PackageError("package must be a plain name from the default registry, "
                           "not a URL or VCS reference")
    if p.startswith(("-", ".", "/", "~", "_")) or ".." in p:
        raise PackageError("package must be a plain name, not a flag or a path")
    if "/" in p and not (manager == "npm" and p.startswith("@") and p.count("/") == 1):
        raise PackageError("package must be a plain name, not a path "
                           "(only an npm @scope/name may contain '/')")
    if "=" in p or "@" in p[1:]:
        raise PackageError("put the version in `version`, not in the package name")
    if not _NAME_RE[manager].match(p):
        raise PackageError(f"{p!r} is not a valid {manager} package name")
    if manager == "pip":
        p = re.sub(r"[-_.]+", "-", p).lower()          # PEP 503 normalisation
    return p


def validate_version(manager: str, version: str | None) -> str | None:
    """An exact version, or None for "whatever the registry has now" (the
    dry-run pins it before approval). PackageError on anything else. Pure."""
    if version is None or version == "":
        return None
    if not isinstance(version, str):
        raise PackageError("version must be a string")
    v = version.strip()
    if v != version or set(v) & _FORBIDDEN:
        raise PackageError("version contains whitespace or shell syntax")
    if v.startswith(("=", "<", ">", "~", "^", "!")):
        raise PackageError("version must be one exact version (no ranges or operators)")
    if not _VERSION_RE[manager].match(v):
        raise PackageError(f"{v!r} is not an exact {manager} version")
    return v


def check_requested_command(cmd: str | None) -> str:
    """The agent's own command string, bounded, stored for the operator only.
    Refused when it asks for a non-default source (index/registry flags, URLs,
    VCS, local files): the canonical command would silently drop that, so the
    request would not mean what the agent thinks. Never executed."""
    if cmd is None:
        return ""
    if not isinstance(cmd, str):
        raise PackageError("install_command must be a string")
    c = cmd.strip()
    if len(c) > COMMAND_MAX:
        raise PackageError(f"install_command is longer than {COMMAND_MAX} chars")
    if any(ord(ch) < 32 for ch in c):
        raise PackageError("install_command contains control characters")
    if _SOURCE_FLAG_RE.search(c):
        raise PackageError("install_command names a non-default source (an index, "
                           "registry, URL, VCS or local path). Only the default "
                           "registry is supported; request the plain package name.")
    return c


def canonical_argv(manager: str, package: str, version: str | None) -> list[str]:
    """The command the image builder runs, built from VALIDATED fields only.
    `version` should be the resolved (dry-run) version; None only before
    resolution, for display. Pure."""
    if manager == "apt":
        return ["apt-get", "install", "-y", "--no-install-recommends",
                f"{package}={version}" if version else package]
    if manager == "pip":
        return [PIP_BIN, "install", "--no-input", "--disable-pip-version-check",
                f"{package}=={version}" if version else package]
    if manager == "npm":
        return ["npm", "install", "--global", "--prefix", NPM_PREFIX,
                "--no-audit", "--no-fund",
                f"{package}@{version}" if version else package]
    raise PackageError(f"unknown manager {manager!r}")


def canonical_command(manager: str, package: str, version: str | None) -> str:
    return shlex.join(canonical_argv(manager, package, version))


def validate_request(manager: str, package: str, version: str | None = None,
                     install_command: str | None = None,
                     reason: str | None = None) -> dict:
    """Validate a whole request; returns the normalised fields. Pure."""
    name = validate_name(manager, package)
    ver = validate_version(manager, version)
    cmd = check_requested_command(install_command)
    r = (reason or "").strip()
    if not r:
        raise PackageError("reason is required: say what the package is for")
    if len(r) > REASON_MAX:
        raise PackageError(f"reason is longer than {REASON_MAX} chars")
    return {"manager": manager, "package": name, "version_req": ver,
            "requested_command": cmd, "reason": r,
            "canonical_command": canonical_command(manager, name, ver)}


def check_transition(old: str, new: str) -> None:
    if new not in TRANSITIONS.get(old, frozenset()):
        raise PackageError(f"cannot move a package from {old} to {new}")


# --- catalogue --------------------------------------------------------------

def _row(r) -> dict:
    return dict(r) if r is not None else None


async def project_variant(db, slug: str | None) -> str:
    """The image variant a project's profile uses (projects.profile_id, else
    the marked default, else 'main')."""
    if slug:
        async with db.execute(
                "SELECT sp.box_image FROM projects p JOIN security_profiles sp "
                "ON sp.id = p.profile_id WHERE p.slug = ?", (slug,)) as cur:
            r = await cur.fetchone()
        if r is not None and r[0]:
            return r[0]
    async with db.execute("SELECT box_image FROM security_profiles "
                          "WHERE is_default = 1") as cur:
        r = await cur.fetchone()
    return (r[0] if r is not None and r[0] else "main")


async def project_allows_requests(db, slug: str | None) -> bool | None:
    """The profile's allow_package_requests (the project's, else the marked
    default's)."""
    row = None
    if slug:
        async with db.execute(
                "SELECT sp.allow_package_requests FROM projects p JOIN security_profiles sp "
                "ON sp.id = p.profile_id WHERE p.slug = ?", (slug,)) as cur:
            row = await cur.fetchone()
    if row is None:
        # the marked default; the first use creates it when setup did not
        from . import profiles
        return bool((await profiles.default(db))["allow_package_requests"])
    return bool(row[0])


async def variant_used_by(db, variant: str) -> dict:
    """Which projects a change to `variant` reaches: the ones whose profile
    uses it directly, plus the ones on a variant built FROM it (they inherit
    its packages). {"direct": [slugs], "via": {variant: [slugs]}, "all": [...]}"""
    from .vm import images
    descendants = await images.descendants(db, variant)
    async with db.execute(
            "SELECT p.slug, COALESCE(sp.box_image, d.box_image, 'main') AS v "
            "FROM projects p LEFT JOIN security_profiles sp ON sp.id = p.profile_id "
            "LEFT JOIN security_profiles d ON d.is_default = 1 "
            "WHERE COALESCE(p.deleted_at, '') = '' ORDER BY p.slug") as cur:
        rows = [(r[0], r[1]) for r in await cur.fetchall()]
    direct = [s for s, v in rows if v == variant]
    via: dict[str, list[str]] = {}
    for s, v in rows:
        if v != variant and v in descendants:
            via.setdefault(v, []).append(s)
    everyone = sorted(set(direct) | {s for ss in via.values() for s in ss})
    return {"direct": direct, "via": via, "all": everyone}


def approval_card(row: dict, used: dict) -> str:
    """The one line the approval card leads with (operator decision 0.2)."""
    v = row.get("target_variant") or "?"
    who = ", ".join(used["direct"]) or "no projects yet"
    extra = "".join(f"; via `{k}`: {', '.join(s)}" for k, s in sorted(used["via"].items()))
    return f"installs into `{v}` — used by: {who}{extra}"


async def get(db, pkg_id: int) -> dict | None:
    async with db.execute("SELECT * FROM package_catalogue WHERE id = ?",
                          (pkg_id,)) as cur:
        return _row(await cur.fetchone())


async def list_rows(db, status: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM package_catalogue", ()
    if status:
        if status not in STATUSES:
            raise PackageError(f"unknown status {status!r}")
        q, args = q + " WHERE status = ?", (status,)
    async with db.execute(q + " ORDER BY id DESC LIMIT 500", args) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    cache: dict[str, dict] = {}
    for r in rows:
        v = r.get("target_variant") or "main"
        if v not in cache:
            cache[v] = await variant_used_by(db, v)
        r["variant_used_by"] = cache[v]["all"]
        r["variant_used_by_detail"] = cache[v]
        r["card"] = approval_card({**r, "target_variant": v}, cache[v])
    return rows


async def file_request(db, *, manager: str, package: str, version: str | None,
                       install_command: str | None, reason: str,
                       project: str | None, conversation_id: int | None,
                       source: str = "agent",
                       target_variant: str | None = None) -> dict:
    """Validate and store a request (status pending). Raises PackageError."""
    fields = validate_request(manager, package, version, install_command, reason)
    if source not in ("agent", "operator"):
        raise PackageError("bad source")
    if source == "agent":
        allowed = await project_allows_requests(db, project)
        if allowed is False:
            raise PackageError("this project's security profile does not allow "
                               "package requests")
        target_variant = await project_variant(db, project)
    else:
        from .vm import images
        target_variant = images.check_variant_name(target_variant or "main")
    async with db.execute(
            "SELECT id, status FROM package_catalogue WHERE manager = ? AND package = ? "
            "AND COALESCE(version_req, '') = ? AND COALESCE(target_variant, '') = ? "
            "AND status IN ('pending', 'approved', 'building', 'built')",
            (fields["manager"], fields["package"], fields["version_req"] or "",
             target_variant)) as cur:
        dup = await cur.fetchone()
    if dup is not None:
        raise PackageError(f"{fields['package']} is already in the catalogue for "
                           f"`{target_variant}` (#{dup[0]}, {dup[1]})")
    cur = await db.execute(
        "INSERT INTO package_catalogue(project_slug, source, manager, package, "
        "version_req, requested_command, canonical_command, reason, conversation_id, "
        "status, target_variant) VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?)",
        (project, source, fields["manager"], fields["package"], fields["version_req"],
         fields["requested_command"], fields["canonical_command"], fields["reason"],
         conversation_id, target_variant))
    await db.commit()
    row = await get(db, cur.lastrowid)
    used = await variant_used_by(db, target_variant)
    await security.raise_event(
        db, kind="package_requested", severity="info", project=project,
        summary=f"{source} requested {manager} package {fields['package']}"
                f"{' ' + fields['version_req'] if fields['version_req'] else ''} "
                f"for `{target_variant}`",
        detail={"package_id": row["id"], "manager": manager,
                "package": fields["package"], "version": fields["version_req"],
                "reason": fields["reason"],
                "requested_command": fields["requested_command"],
                "canonical_command": fields["canonical_command"],
                "target_variant": target_variant, "variant_used_by": used["all"],
                "conversation_id": conversation_id, "source": source})
    return row


async def set_resolution(db, pkg_id: int, *, resolved_version: str | None,
                         integrity: str | None, error: str | None = None) -> None:
    """Record the dry-run result. The resolved version is re-validated (it came
    from a guest) and the canonical command is rebuilt around it."""
    row = await get(db, pkg_id)
    if row is None or row["status"] != "pending":
        return
    try:
        ver = validate_version(row["manager"], resolved_version) if resolved_version else None
    except PackageError:
        ver, error = None, error or "resolver returned an invalid version"
    if row["version_req"] and ver and ver != row["version_req"]:
        ver, error = None, f"requested {row['version_req']} but resolved {ver}"
    integ = (integrity or "")[:300] if ver else None
    if not ver:
        integ = f"unresolved: {(error or 'not found')[:200]}"
    await db.execute(
        "UPDATE package_catalogue SET resolved_version = ?, integrity = ?, "
        "canonical_command = ? WHERE id = ?",
        (ver, integ, canonical_command(row["manager"], row["package"],
                                       ver or row["version_req"]), pkg_id))
    await db.commit()


async def approve(db, pkg_id: int, *, target_variant: str | None,
                  decided_by: str = "unmarked", by_operator: bool = False) -> dict:
    """pending -> approved. Needs a successful dry-run. Does NOT build; the
    caller (packages_api) asks images for a new variant version."""
    row = await get(db, pkg_id)
    if row is None:
        raise LookupError("no such package request")
    check_transition(row["status"], "approved")
    if not row.get("resolved_version"):
        raise PackageError("not resolved yet: the dry-run must pin a version and "
                           "integrity before approval")
    from .vm import images
    variant = images.check_variant_name(target_variant or row["target_variant"] or "main")
    if not await images.variant_exists(db, variant):
        raise PackageError(f"no image variant `{variant}`")
    await db.execute(
        "UPDATE package_catalogue SET status = 'approved', target_variant = ?, "
        "decided_by = ?, decided_at = datetime('now') WHERE id = ?",
        (variant, decided_by, pkg_id))
    await db.commit()
    row = await get(db, pkg_id)
    used = await variant_used_by(db, variant)
    await security.raise_event(
        db, kind="package_approved", severity="warn", project=row["project_slug"],
        summary=f"approved {row['manager']} {row['package']}=={row['resolved_version']} "
                f"into `{variant}` (used by: {', '.join(used['all']) or 'none'})",
        detail={"package_id": pkg_id, "target_variant": variant,
                "variant_used_by": used["all"], "integrity": row["integrity"],
                "canonical_command": row["canonical_command"]},
        actor=security.OPERATOR if by_operator else None)
    return row


async def reject(db, pkg_id: int, *, reason: str = "",
                 decided_by: str = "unmarked", by_operator: bool = False) -> dict:
    row = await get(db, pkg_id)
    if row is None:
        raise LookupError("no such package request")
    check_transition(row["status"], "rejected")
    await db.execute(
        "UPDATE package_catalogue SET status = 'rejected', decided_by = ?, "
        "decided_at = datetime('now') WHERE id = ?", (decided_by, pkg_id))
    await db.commit()
    await security.raise_event(
        db, kind="package_rejected", severity="warn", project=row["project_slug"],
        summary=f"rejected {row['manager']} package {row['package']}",
        detail={"package_id": pkg_id, "reason": (reason or "")[:500]},
        actor=security.OPERATOR if by_operator else None)
    return await get(db, pkg_id)


async def remove(db, pkg_id: int) -> dict:
    row = await get(db, pkg_id)
    if row is None:
        raise LookupError("no such package request")
    check_transition(row["status"], "removed")
    await db.execute("UPDATE package_catalogue SET status = 'removed' WHERE id = ?",
                     (pkg_id,))
    await db.commit()
    return await get(db, pkg_id)


async def set_status(db, ids: list[int], new: str, *,
                     built_version: int | None = None) -> None:
    """Move rows through building/built/failed (the image builder calls this).
    Rows not in a state that allows `new` are left alone."""
    for pid in ids:
        row = await get(db, pid)
        if row is None or new not in TRANSITIONS.get(row["status"], frozenset()):
            continue
        await db.execute(
            "UPDATE package_catalogue SET status = ?, "
            "built_version = COALESCE(?, built_version) WHERE id = ?",
            (new, built_version, pid))
    await db.commit()


async def approved_for(db, variant: str) -> list[dict]:
    """Catalogue rows that belong in `variant`'s recipe."""
    async with db.execute(
            "SELECT * FROM package_catalogue WHERE target_variant = ? AND status IN "
            "('approved', 'building', 'built', 'failed') ORDER BY id", (variant,)) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def unresolved(db) -> list[dict]:
    async with db.execute(
            "SELECT * FROM package_catalogue WHERE status = 'pending' AND "
            "resolved_version IS NULL ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]
