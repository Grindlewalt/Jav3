"""Service boxes: approved, long-lived services (DESIGN-BOXES.md (a), WP3).

The agent files a *service request* (tool `service_request`). It never starts
anything: the operator approves an immutable, content-hashed artifact over the
cookie-only API (services_api.py), choosing the PLACEMENT explicitly (operator
decision 0.1) and which requested ports are exposed, where (decision 0.4).

What an approved service is, host-side:

  * a row in `services` (the definition: argv, env, ports, egress hosts ...);
  * a tar of the files it runs, snapshotted by the HOST from the canonical
    project tree (never from a guest) at <vm_dir>/svc/<slug>/<id>/<sha256>.tar,
    refused if any file carries a secret value;
  * a data disk (the only thing that persists), mounted at /srv in the box,
    capped (vm_svc_data_max_mb) and `nodev,nosuid,noexec`.

Where it runs: a *service box* (boxes.py kind "service") picked by placement:
per_project `s-<slug>`, per_service `s-<slug>-<id>`, shared `s-shared`. A
service box has no workspace, no op tokens and no model/broker access (the
gateway gates it by CID, boxes.GATEWAY_OPS). Its root disk is a fresh overlay
every boot (this module deletes any stale overlay before a boot), so a rootkit
in /usr or a planted unit dies on restart: persistence lives in host-held
definitions (re-applied every boot) plus /srv state, never in guest config.

In the box, `svcd` (guest/svc/svcd.py, served as the service box's package by
svc_pkg.py) runs each approved service as a hardened transient systemd unit and
answers the host over the box transport on port 5558 (host-dialed only).

Egress: deny-by-default, whatever the profile says. The effective allow list is
the union of the box's running services' approved `egress_hosts` minus the
profile and project deny lists (`egress_decide`, for the proxy / WP2). No auto
mode, no queue training from service traffic.

Inbound: none by default. An approved exposure runs a host-side metered relay
(portfwd.py) that reaches the service through svcd's tunnel op — never a
network path into the box. Loopback, or the dedicated `services_lan_ip`.

Revoke: delete the definition and destroy the box (killing QEMU is the
authoritative stop); the reconcile brings it back for the services that remain.
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import io
import ipaddress
import json
import re
import tarfile
import time
from pathlib import Path, PurePosixPath

from ..config import settings
from . import boxes

SVC_VARIANT = "svc"                 # the image variant service boxes boot (WP5)
PLACEMENTS = boxes.PLACEMENTS
RESTARTS = ("no", "on-failure", "always")
EXPOSES = ("none", "host", "lan")
BINDS = ("loopback", "lan")

# request limits (agent-authored input: every one is a refusal, not a clamp)
MAX_FILES = 2000
MAX_BYTES = 50 * 1024 * 1024
MAX_PATTERNS = 64
MAX_ARGV = 64
MAX_ARG_LEN = 1024
MAX_ENV = 64
MAX_ENV_VAL = 4096
MAX_PORTS = 8
MAX_HOSTS = 32
MAX_LOG_LINES = 500
_LOG_CAP = 64 * 1024

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)"
                      r"(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")
# env the service may not set: the loader, proxy routing (the network forces
# the proxy anyway; these would only make it try direct and log drops), and
# what svcd / systemd set for it.
_ENV_RESERVED_PREFIX = ("LD_", "SYSTEMD_", "DBUS_")
_ENV_RESERVED = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY",
    "SRV", "STATE_DIRECTORY", "RUNTIME_DIRECTORY", "CREDENTIALS_DIRECTORY",
    "NOTIFY_SOCKET", "INVOCATION_ID", "LISTEN_FDS", "LISTEN_PID",
    "PYTHONSTARTUP", "PYTHONPATH", "NODE_OPTIONS", "BASH_ENV", "ENV"})
_RESERVED_PORTS = frozenset({5555, 5556, 5557, 5558, 8443})
_SECRET_MARK = "{{secret:"

# svcd transport
QMP_ROOT_PORT = "jpersist_rp"       # the spare hotplug port run_vm.sh reserves
_SRV_SERIAL, _IMPORT_SERIAL = "jsrv", "jimport"
_RPC_TIMEOUT = 30.0
_APPLY_TIMEOUT = 180.0
_UNREPORTED_AFTER = 3               # missed pings before svc_unreported
_RESTART_BACKOFF = (30, 60, 120, 300, 900)


class ServiceError(Exception):
    """A refused request or action. `status` is the HTTP code the API uses."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


# --- validation (pure) -----------------------------------------------------------

def _no_secrets(text: str, where: str) -> None:
    """Refuse a secret placeholder or a secret VALUE anywhere in `text`.
    Service boxes never hold secrets: HTTP wire injection through the proxy
    stays the only secret path. Names only in the message, never values."""
    if _SECRET_MARK in text:
        raise ServiceError(f"{where}: '{{{{secret:...}}}}' placeholders are not "
                           "allowed in services (a service box never holds a "
                           "secret value)")
    from .. import secrets
    hits = secrets.find_in_bytes(text.encode())
    if hits:
        raise ServiceError(f"{where} contains the value of secret(s) "
                           f"{', '.join(hits)}; refusing to file it")


def _printable(s: str) -> bool:
    return all(c.isprintable() or c == "\t" for c in s)


def _relpath(p, where: str, *, allow_glob: bool) -> str:
    if not isinstance(p, str) or not p.strip() or len(p) > 256 or "\x00" in p:
        raise ServiceError(f"{where}: bad path {p!r}")
    p = p.strip()
    if p.startswith(("/", "~")) or "\\" in p:
        raise ServiceError(f"{where}: {p!r} must be relative to the project")
    parts = PurePosixPath(p).parts
    if any(x == ".." for x in parts):
        raise ServiceError(f"{where}: {p!r} may not contain '..'")
    if any(x == ".git" for x in parts):
        raise ServiceError(f"{where}: {p!r} is inside .git")
    if not allow_glob and any(c in p for c in "*?["):
        raise ServiceError(f"{where}: {p!r} may not be a glob")
    return str(PurePosixPath(p))


def validate_request(args: dict) -> dict:
    """The canonical definition for a service_request, or ServiceError.
    Everything the operator approves is in this dict (plus the artifact
    hash); nothing else about the service comes from the agent."""
    if not isinstance(args, dict):
        raise ServiceError("arguments must be an object")
    name = args.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ServiceError("name: lowercase letters, digits and '-', starting "
                           "with a letter, at most 40")
    desc = args.get("description") or ""
    reason = args.get("reason") or ""
    for k, v, cap in (("description", desc, 500), ("reason", reason, 1000)):
        if not isinstance(v, str) or len(v) > cap or not _printable(v.replace("\n", " ")):
            raise ServiceError(f"{k}: a string of at most {cap} characters")
    if not reason.strip():
        raise ServiceError("reason: say why this service needs to persist")

    cmd = args.get("command")
    if (not isinstance(cmd, list) or not 1 <= len(cmd) <= MAX_ARGV
            or not all(isinstance(a, str) for a in cmd)):
        raise ServiceError(f"command: an argv list of 1..{MAX_ARGV} strings "
                           "(not a shell string)")
    if not cmd[0].strip():
        raise ServiceError("command: argv[0] is empty")
    for a in cmd:
        if len(a) > MAX_ARG_LEN or "\x00" in a or not _printable(a):
            raise ServiceError("command: arguments must be printable and at "
                               f"most {MAX_ARG_LEN} characters")
    workdir = _relpath(args.get("workdir") or ".", "workdir", allow_glob=False)

    files = args.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_PATTERNS:
        raise ServiceError(f"files: 1..{MAX_PATTERNS} project paths or globs")
    files = sorted({_relpath(f, "files", allow_glob=True) for f in files})

    ports_in = args.get("ports") or []
    if not isinstance(ports_in, list) or len(ports_in) > MAX_PORTS:
        raise ServiceError(f"ports: at most {MAX_PORTS}")
    ports, seen = [], set()
    for p in ports_in:
        if not isinstance(p, dict):
            raise ServiceError("ports: each entry is {port, protocol, purpose, expose}")
        port = p.get("port")
        if (not isinstance(port, int) or isinstance(port, bool)
                or not 1024 <= port <= 65535 or port in _RESERVED_PORTS):
            raise ServiceError(f"ports: {port!r} must be 1024..65535 and not "
                               "one of Jav3's own")
        if port in seen:
            raise ServiceError(f"ports: {port} listed twice")
        seen.add(port)
        proto = p.get("protocol", "tcp")
        if proto != "tcp":
            raise ServiceError("ports: only tcp is supported")
        expose = p.get("expose", "none")
        if expose not in EXPOSES:
            raise ServiceError(f"ports: expose must be one of {', '.join(EXPOSES)}")
        purpose = p.get("purpose") or ""
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 200:
            raise ServiceError("ports: each port needs a purpose (<= 200 chars)")
        ports.append({"port": port, "protocol": "tcp", "purpose": purpose.strip(),
                      "expose": expose})
    ports.sort(key=lambda x: x["port"])

    restart = args.get("restart") or "no"
    if restart not in RESTARTS:
        raise ServiceError(f"restart: one of {', '.join(RESTARTS)}")

    hosts_in = args.get("egress_hosts") or []
    if not isinstance(hosts_in, list) or len(hosts_in) > MAX_HOSTS:
        raise ServiceError(f"egress_hosts: at most {MAX_HOSTS} hostnames")
    hosts = set()
    for h in hosts_in:
        h = h.strip().lower().rstrip(".") if isinstance(h, str) else ""
        try:
            ipaddress.ip_address(h)
            raise ServiceError(f"egress_hosts: {h!r} is an IP address; name hosts")
        except ValueError:
            pass
        if not _HOST_RE.match(h):
            raise ServiceError(f"egress_hosts: {h!r} is not a hostname")
        hosts.add(h)

    env_in = args.get("env") or {}
    if not isinstance(env_in, dict) or len(env_in) > MAX_ENV:
        raise ServiceError(f"env: an object of at most {MAX_ENV} entries")
    env = {}
    for k, v in env_in.items():
        if not isinstance(k, str) or not _ENV_KEY_RE.match(k):
            raise ServiceError(f"env: key {k!r} must be UPPER_SNAKE_CASE")
        if k in _ENV_RESERVED or k.startswith(_ENV_RESERVED_PREFIX):
            raise ServiceError(f"env: {k} is reserved")
        if not isinstance(v, str) or len(v) > MAX_ENV_VAL or not _printable(v):
            raise ServiceError(f"env: {k} must be a printable string of at most "
                               f"{MAX_ENV_VAL} characters")
        _no_secrets(v, f"env {k}")
        env[k] = v

    canon = {"name": name, "description": desc.strip(), "command": cmd,
             "workdir": workdir, "files": files, "ports": ports,
             "restart": restart, "egress_hosts": sorted(hosts), "env": env,
             "reason": reason.strip()}
    _no_secrets(json.dumps(canon), "the request")
    return canon


# --- the artifact (host snapshot of canonical project files) ----------------------

def project_root(slug: str) -> Path:
    return settings.projects_dir / slug


def _collect(root: Path, patterns: list[str]) -> list[tuple[str, Path]]:
    """(relative posix path, file) for every file the patterns name, sorted.
    Symlinks are refused (a link is a pointer out of the project the operator
    can't review); .git is never included."""
    root_r = root.resolve()
    out: dict[str, Path] = {}
    for pat in patterns:
        matched = False
        cands = [root / pat] if not any(c in pat for c in "*?[") else root.glob(pat)
        for c in cands:
            if not c.exists() and not c.is_symlink():
                continue
            matched = True
            walk = [c] if not c.is_dir() or c.is_symlink() else sorted(c.rglob("*"))
            for f in walk:
                rel = f.relative_to(root).as_posix()
                if ".git" in PurePosixPath(rel).parts:
                    continue
                if f.is_symlink():
                    raise ServiceError(f"files: {rel} is a symlink; refusing it")
                if f.is_dir():
                    continue
                if not f.resolve().is_relative_to(root_r):
                    raise ServiceError(f"files: {rel} resolves outside the project")
                out[rel] = f
        if not matched:
            raise ServiceError(f"files: {pat!r} matches nothing in the project")
    if len(out) > MAX_FILES:
        raise ServiceError(f"files: {len(out)} files, over the {MAX_FILES} cap")
    return sorted(out.items())


def snapshot(slug: str, patterns: list[str]) -> tuple[bytes, list[dict]]:
    """A deterministic tar of the named project files and its manifest. The
    same tree always hashes the same (sorted, mtime/uid zeroed, modes
    normalised), so a re-request of unchanged code has the same sha256.
    Refuses any file that carries a secret value."""
    from .. import secrets
    root = project_root(slug)
    if not root.is_dir():
        raise ServiceError(f"project '{slug}' has no files on disk", 404)
    items = _collect(root, patterns)
    buf, total, manifest = io.BytesIO(), 0, []
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, f in items:
            data = f.read_bytes()
            total += len(data)
            if total > MAX_BYTES:
                raise ServiceError(f"files: over the {MAX_BYTES // 2**20} MB cap")
            hits = secrets.find_in_bytes(data)
            if hits:
                raise ServiceError(f"files: {rel} contains the value of secret(s) "
                                   f"{', '.join(hits)}; refusing to snapshot it")
            ti = tarfile.TarInfo(rel)
            ti.size = len(data)
            ti.mtime = 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mode = 0o755 if f.stat().st_mode & 0o111 else 0o644
            tar.addfile(ti, io.BytesIO(data))
            manifest.append({"path": rel, "bytes": len(data),
                             "sha256": hashlib.sha256(data).hexdigest()})
    return buf.getvalue(), manifest


def svc_dir() -> Path:
    return settings.vm_dir / "svc"


def artifact_path(slug: str, sid: int, sha: str) -> Path:
    boxes._slug(slug)
    if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        raise ServiceError("bad artifact hash")
    return svc_dir() / slug / str(int(sid)) / f"{sha}.tar"


def definition(canon: dict, artifact_sha: str) -> dict:
    keys = ("name", "description", "command", "workdir", "files", "ports",
            "restart", "egress_hosts", "env")
    return {**{k: canon[k] for k in keys}, "artifact_sha256": artifact_sha}


def definition_sha(defn: dict) -> str:
    return hashlib.sha256(json.dumps(defn, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def verify_artifact(row: dict) -> bytes:
    """The artifact's bytes, re-hashed. A file that no longer matches the
    approved hash (tampered on disk) is refused, never shipped."""
    p = Path(row["artifact_path"] or "")
    try:
        data = p.read_bytes()
    except OSError:
        raise ServiceError(f"service #{row['id']}: artifact is missing", 409) from None
    if hashlib.sha256(data).hexdigest() != row["artifact_sha256"]:
        raise ServiceError(f"service #{row['id']}: artifact does not match its "
                           "approved hash", 409)
    return data


# --- rows ------------------------------------------------------------------------

_JSON_COLS = {"command": [], "files": [], "ports": [], "egress_hosts": [],
              "env": {}, "expose_ports": []}

# runtime truth per service id (not persisted: a restart re-derives it)
_runtime: dict[int, dict] = {}

# the `services` topic on /api/events (the lists refetch on either event):
#   {"type": "service_changed", service_id, project, status, desired_state}
#       a definition was filed, approved, rejected, started/stopped or revoked
#   {"type": "service_state", service_id, state, error}
#       the host's view of a unit changed (running/stopped/failed/unreported)
BUS_CHAN = "services"


def _publish(ev: dict) -> None:
    from .. import bus
    bus.publish(BUS_CHAN, ev)


def _changed(row: dict | None) -> None:
    if row:
        _publish({"type": "service_changed", "service_id": row["id"],
                  "project": row["project_slug"], "status": row["status"],
                  "desired_state": row["desired_state"]})


def _set_runtime(sid: int, state: str, error: str | None = None) -> None:
    """Record a unit's state; publish only a change (the pinger runs every few
    seconds and must not flood the stream)."""
    new = {"state": state, "error": error}
    if _runtime.get(sid) != new:
        _runtime[sid] = new
        _publish({"type": "service_state", "service_id": sid, "state": state,
                  "error": error})


def _decode(row) -> dict:
    d = dict(row)
    for k, dflt in _JSON_COLS.items():
        try:
            d[k] = json.loads(d.get(k) or "null") or dflt
        except ValueError:
            d[k] = dflt
    return d


def state_of(row: dict) -> str:
    """running | stopped | failed | unreported (host truth where it has it)."""
    if row["status"] != "approved" or row["desired_state"] != "running":
        return "stopped"
    rt = _runtime.get(row["id"])
    if not rt:
        return "stopped"
    return rt.get("state", "stopped")


def row_json(row: dict) -> dict:
    keys = ("id", "project_slug", "name", "description", "command", "workdir",
            "files", "ports", "restart", "egress_hosts", "env", "reason",
            "artifact_sha256", "definition_sha256", "placement", "expose_ports",
            "status", "desired_state", "supersedes_id", "last_reported_at",
            "created_at", "decided_at", "decided_by", "decision_note",
            "requested_by", "conversation_id")
    out = {k: row.get(k) for k in keys}
    out["box_id"] = box_id_of(row) if row["status"] == "approved" else None
    out["state"] = state_of(row)
    rt = _runtime.get(row["id"]) or {}
    out["error"] = rt.get("error")
    return out


async def _fetch(db, sid: int) -> dict | None:
    async with db.execute("SELECT * FROM services WHERE id = ?", (sid,)) as cur:
        r = await cur.fetchone()
    return _decode(r) if r else None


async def get(sid: int) -> dict | None:
    from ..db import get_db
    db = await get_db()
    try:
        return await _fetch(db, sid)
    finally:
        await db.close()


async def list_services(project: str | None = None, *,
                        statuses: tuple[str, ...] | None = None) -> list[dict]:
    from ..db import get_db
    q, args = "SELECT * FROM services", []
    conds = []
    if project:
        conds.append("project_slug = ?")
        args.append(project)
    if statuses:
        conds.append(f"status IN ({','.join('?' * len(statuses))})")
        args.extend(statuses)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    db = await get_db()
    try:
        async with db.execute(q + " ORDER BY id DESC", args) as cur:
            return [_decode(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _event(kind: str, summary: str, *, severity: str = "warn",
                 project: str | None = None, detail: dict | None = None) -> None:
    from .. import security
    from ..db import get_db
    db = await get_db()
    try:
        await security.raise_event(db, kind=kind, severity=severity,
                                   project=project, summary=summary, detail=detail)
    finally:
        await db.close()


# --- filing (the agent's side) -------------------------------------------------------

async def file_request(slug: str, args: dict, *, conversation_id: int | None = None,
                       requested_by: str = "agent") -> dict:
    """Validate, snapshot and file one request. Never starts anything."""
    if not settings.vm_boxes_enabled:
        raise ServiceError("services need service boxes, which are off on this "
                           "host (vm_boxes_enabled)", 409)
    canon = validate_request(args)
    prof = await boxes.project_profile(slug)
    if not prof or not prof.get("allow_services"):
        raise ServiceError("this project's security profile does not allow "
                           "services (ask the operator)", 403)
    placement = prof.get("service_placement")
    if placement not in PLACEMENTS:
        # decision 0.1: never inferred. A profile without one cannot file.
        raise ServiceError("this project's security profile has no service "
                           "placement set; the operator must choose one", 409)
    tar, manifest = snapshot(slug, canon["files"])
    wd = canon["workdir"]
    if wd != "." and not any(m["path"].startswith(wd + "/") for m in manifest):
        raise ServiceError(f"workdir: {wd!r} holds none of the snapshotted files")
    sha = hashlib.sha256(tar).hexdigest()
    defn = definition(canon, sha)
    dsha = definition_sha(defn)

    from ..db import get_db
    db = await get_db()
    try:
        async with db.execute(
                "SELECT id, status, definition_sha256 FROM services WHERE "
                "project_slug = ? AND name = ? AND status IN ('pending','approved') "
                "ORDER BY id DESC", (slug, canon["name"])) as cur:
            live = [dict(r) for r in await cur.fetchall()]
        for r in live:
            if r["status"] == "pending":
                raise ServiceError(f"service request #{r['id']} for '{canon['name']}' "
                                   "is already pending; wait for the operator", 409)
            if r["definition_sha256"] == dsha:
                raise ServiceError(f"service #{r['id']} '{canon['name']}' is already "
                                   "approved with exactly this definition", 409)
        supersedes = next((r["id"] for r in live if r["status"] == "approved"), None)
        cur = await db.execute(
            "INSERT INTO services (project_slug, name, description, command, workdir,"
            " files, ports, restart, egress_hosts, env, reason, artifact_sha256,"
            " definition_sha256, placement, supersedes_id, conversation_id,"
            " requested_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (slug, canon["name"], canon["description"], json.dumps(canon["command"]),
             canon["workdir"], json.dumps(canon["files"]), json.dumps(canon["ports"]),
             canon["restart"], json.dumps(canon["egress_hosts"]),
             json.dumps(canon["env"]), canon["reason"], sha, dsha, placement,
             supersedes, conversation_id, requested_by))
        sid = cur.lastrowid
        path = artifact_path(slug, sid, sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(tar)
        tmp.chmod(0o400)
        tmp.replace(path)
        await db.execute("UPDATE services SET artifact_path = ? WHERE id = ?",
                         (str(path), sid))
        await db.commit()
        row = await _fetch(db, sid)
    finally:
        await db.close()
    await _event("service_requested", severity="info", project=slug,
                 summary=f"service '{canon['name']}' requested for '{slug}' "
                         f"(#{sid}{f', replaces #{supersedes}' if supersedes else ''})",
                 detail={"service_id": sid, "name": canon["name"],
                         "artifact_sha256": sha, "files": len(manifest),
                         "ports": canon["ports"], "egress_hosts": canon["egress_hosts"],
                         "placement": placement, "supersedes_id": supersedes})
    _changed(row)
    return row


# --- the operator's decisions -----------------------------------------------------------

def check_exposure(row: dict, expose_ports: list) -> list[dict]:
    """The operator's exposure choice, validated against the request: only a
    port the agent asked to expose, bound no wider than it asked (host ->
    loopback only; lan -> loopback or lan), lan only while services_lan_ip is
    set and is not an address Jav3 itself uses."""
    if not isinstance(expose_ports, list):
        raise ServiceError("expose_ports: a list of {port, bind}", 422)
    asked = {p["port"]: p["expose"] for p in row["ports"]}
    out, seen = [], set()
    for e in expose_ports:
        if not isinstance(e, dict):
            raise ServiceError("expose_ports: each entry is {port, bind}", 422)
        port, bind = e.get("port"), e.get("bind")
        if port not in asked or asked[port] == "none":
            raise ServiceError(f"expose_ports: port {port!r} was not requested for "
                               "exposure")
        if port in seen:
            raise ServiceError(f"expose_ports: port {port} twice")
        seen.add(port)
        if bind not in BINDS:
            raise ServiceError("expose_ports: bind is 'loopback' or 'lan'")
        if bind == "lan":
            if asked[port] != "lan":
                raise ServiceError(f"expose_ports: port {port} was requested for "
                                   "the host only, not the LAN")
            from . import portfwd
            try:
                portfwd.lan_address()
            except portfwd.PortfwdError as ex:
                raise ServiceError(f"expose_ports: {ex}", 409) from None
        out.append({"port": port, "bind": bind})
    return sorted(out, key=lambda x: x["port"])


async def approve(sid: int, *, placement: str, expose_ports: list,
                  by: str = "operator") -> dict:
    """pending -> approved. `placement` is required and explicit (decision
    0.1); `expose_ports` is the operator's exposure choice (may be empty).
    Compare-and-set on status so a double click cannot approve twice."""
    if placement not in PLACEMENTS:
        raise ServiceError(f"placement is required: one of {', '.join(PLACEMENTS)}",
                           422)
    if not settings.vm_boxes_enabled:
        raise ServiceError("service boxes are off (vm_boxes_enabled)", 409)
    from ..db import get_db
    db = await get_db()
    try:
        row = await _fetch(db, sid)
        if row is None:
            raise ServiceError("no such service", 404)
        if row["status"] != "pending":
            raise ServiceError(f"service #{sid} is {row['status']}, not pending", 409)
        exposure = check_exposure(row, expose_ports)
        verify_artifact(row)
        cur = await db.execute(
            "UPDATE services SET status = 'approved', desired_state = 'running',"
            " placement = ?, expose_ports = ?, decided_by = ?,"
            " decided_at = datetime('now'), updated_at = datetime('now')"
            " WHERE id = ? AND status = 'pending'",
            (placement, json.dumps(exposure), by, sid))
        if cur.rowcount != 1:
            raise ServiceError(f"service #{sid} was decided concurrently", 409)
        old = None
        if row["supersedes_id"]:
            old = await _fetch(db, row["supersedes_id"])
            if old and old["status"] == "approved":
                await db.execute(
                    "UPDATE services SET status = 'superseded', desired_state ="
                    " 'stopped', updated_at = datetime('now') WHERE id = ?",
                    (old["id"],))
            else:
                old = None
        await db.commit()
        row = await _fetch(db, sid)
    finally:
        await db.close()
    ports_txt = ", ".join("%s/%s" % (e["port"], e["bind"]) for e in exposure)
    await _event("service_approved", project=row["project_slug"],
                 summary=f"{by} approved service '{row['name']}' (#{sid}) for "
                         f"'{row['project_slug']}' ({placement}"
                         + (f", exposed {ports_txt})" if exposure else ")"),
                 detail={"service_id": sid, "by": by, "placement": placement,
                         "expose_ports": exposure,
                         "artifact_sha256": row["artifact_sha256"],
                         "egress_hosts": row["egress_hosts"],
                         "superseded": old["id"] if old else None})
    _changed(row)
    if old is not None:
        _changed({**old, "status": "superseded", "desired_state": "stopped"})
        _kick(box_id_of(old))
    _kick(box_id_of(row))
    return row


async def reject(sid: int, reason: str = "", by: str = "operator") -> dict:
    from ..db import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "UPDATE services SET status = 'rejected', decided_by = ?, decided_at ="
            " datetime('now'), decision_note = ?, updated_at = datetime('now')"
            " WHERE id = ? AND status = 'pending'", (by, (reason or "")[:500], sid))
        await db.commit()
        row = await _fetch(db, sid)
    finally:
        await db.close()
    if row is None:
        raise ServiceError("no such service", 404)
    if cur.rowcount != 1:
        raise ServiceError(f"service #{sid} is {row['status']}, not pending", 409)
    await _event("service_rejected", severity="info", project=row["project_slug"],
                 summary=f"{by} rejected service '{row['name']}' (#{sid})",
                 detail={"service_id": sid, "by": by, "reason": row["decision_note"]})
    _changed(row)
    return row


async def set_desired(sid: int, state: str, by: str = "operator") -> dict:
    """Start / stop an approved service (the unit; the box follows)."""
    if state not in ("running", "stopped"):
        raise ServiceError("state is running or stopped")
    from ..db import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "UPDATE services SET desired_state = ?, updated_at = datetime('now')"
            " WHERE id = ? AND status = 'approved'", (state, sid))
        await db.commit()
        row = await _fetch(db, sid)
    finally:
        await db.close()
    if row is None:
        raise ServiceError("no such service", 404)
    if cur.rowcount != 1:
        raise ServiceError(f"service #{sid} is {row['status']}, not approved", 409)
    _changed(row)
    _kick(box_id_of(row))
    return row


async def revoke(sid: int, *, delete_data: bool = False, by: str = "operator") -> dict:
    """Delete the definition and DESTROY its box: killing QEMU is the
    authoritative stop, whatever svcd says. The box comes back (fresh root)
    for the services that remain in it. `delete_data` removes this service's
    /srv state (the whole disk when nothing else shares it)."""
    from ..db import get_db
    db = await get_db()
    try:
        row = await _fetch(db, sid)
        if row is None:
            raise ServiceError("no such service", 404)
        if row["status"] not in ("pending", "approved"):
            raise ServiceError(f"service #{sid} is already {row['status']}", 409)
        await db.execute(
            "UPDATE services SET status = 'revoked', desired_state = 'stopped',"
            " revoked_at = datetime('now'), decided_by = COALESCE(decided_by, ?),"
            " updated_at = datetime('now') WHERE id = ?", (by, sid))
        await db.commit()
    finally:
        await db.close()
    was_running = row["status"] == "approved"
    bid = box_id_of(row) if was_running else None
    deleted = None
    if was_running:
        from . import portfwd
        await portfwd.close_service(sid)
        box = boxes.get(bid)
        if box is not None:
            try:
                await boxes.destroy(box)            # stop (kill QEMU) + release
            except Exception as e:  # noqa: BLE001 — the revoke stands regardless
                print(f"[services] destroy {bid}: {e}")
        remaining = [r for r in await list_services(statuses=("approved",))
                     if box_id_of(r) == bid]
        if delete_data:
            if remaining:
                _add_wipe(row_disk(row), sid)        # svcd wipes it next boot
                deleted = "scheduled"
            else:
                deleted = _delete_disk_file(row_disk(row))
        _runtime.pop(sid, None)
        _kick(bid)
    await _event("service_revoked", project=row["project_slug"],
                 summary=f"{by} revoked service '{row['name']}' (#{sid})"
                         + (f"; box {bid} destroyed" if bid else ""),
                 detail={"service_id": sid, "by": by, "box_id": bid,
                         "delete_data": bool(delete_data), "data_deleted": deleted})
    out = await get(sid)
    _changed(out)
    return {**out, "data_deleted": deleted}


def _tar_texts(data: bytes | None) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    if not data:
        return out
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            raw = tar.extractfile(m).read() if m.size <= 512 * 1024 else None
            try:
                out[m.name] = raw.decode() if raw is not None else None
            except UnicodeDecodeError:
                out[m.name] = None
    return out


async def diff(sid: int) -> str | None:
    """Unified diff of this request against the approved one it supersedes:
    the definition first, then every file. None when it supersedes nothing."""
    row = await get(sid)
    if row is None or not row["supersedes_id"]:
        return None
    old = await get(row["supersedes_id"])
    if old is None:
        return None

    def defn_text(r):
        return json.dumps(definition(r, r["artifact_sha256"]), indent=2,
                          sort_keys=True).splitlines(keepends=True)

    def read(r):
        try:
            return Path(r["artifact_path"]).read_bytes()
        except (OSError, TypeError):
            return None

    parts = list(difflib.unified_diff(defn_text(old), defn_text(row),
                                      f"#{old['id']}/definition",
                                      f"#{sid}/definition"))
    a, b = _tar_texts(read(old)), _tar_texts(read(row))
    for name in sorted(set(a) | set(b)):
        x, y = a.get(name), b.get(name)
        if x == y:
            continue
        if (name in a and x is None) or (name in b and y is None):
            parts.append(f"Binary or large file {name} differs\n")
            continue
        parts.extend(difflib.unified_diff(
            (x or "").splitlines(keepends=True), (y or "").splitlines(keepends=True),
            f"a/{name}" if name in a else "/dev/null",
            f"b/{name}" if name in b else "/dev/null"))
    text = "".join(parts)
    return text[:256 * 1024]


# --- boxes, disks --------------------------------------------------------------------

def box_id_of(row: dict) -> str:
    return boxes.box_id_for("service", project=row["project_slug"],
                            service_id=row["id"], placement=row["placement"])


def _box_for(row: dict) -> boxes.Box:
    """Reserve the service's box (idempotent). BoxCapError propagates."""
    return boxes.allocate("service", project=row["project_slug"],
                          service_id=row["id"] if row["placement"] == "per_service"
                          else None,
                          placement=row["placement"], variant=SVC_VARIANT,
                          mem_mb=settings.vm_service_box_mem_mb)


def data_disk(slug: str | None, placement: str, sid: int | None = None) -> Path:
    """The /srv disk of a service box, from its shape (never parsed back out
    of a box id: a slug may itself end in -<digits>).
    per_project svc/<slug>/data.qcow2 ; per_service svc/<slug>/data-<id>.qcow2 ;
    shared svc/_shared/data.qcow2."""
    if placement == "shared":
        return svc_dir() / "_shared" / "data.qcow2"
    boxes._slug(slug)
    if placement == "per_service":
        return svc_dir() / slug / f"data-{int(sid)}.qcow2"
    return svc_dir() / slug / "data.qcow2"


def box_disk(box: boxes.Box) -> Path:
    return data_disk(box.project, box.placement or "per_project", box.service_id)


def row_disk(row: dict) -> Path:
    return data_disk(row["project_slug"], row["placement"], row["id"])


def _ready_marker(disk: Path) -> Path:
    return disk.with_suffix(".ready")


def _delete_disk_file(disk: Path) -> bool:
    existed = disk.exists()
    disk.unlink(missing_ok=True)
    _ready_marker(disk).unlink(missing_ok=True)
    _wipe_path(disk).unlink(missing_ok=True)
    return existed


def _wipe_path(disk: Path) -> Path:
    return disk.with_suffix(".wipe.json")


def _add_wipe(disk: Path, sid: int) -> None:
    p = _wipe_path(disk)
    try:
        ids = set(json.loads(p.read_text()))
    except (OSError, ValueError):
        ids = set()
    ids.add(int(sid))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(ids)))


def _pending_wipes(disk: Path) -> list[int]:
    try:
        return [int(i) for i in json.loads(_wipe_path(disk).read_text())]
    except (OSError, ValueError, TypeError):
        return []


async def ensure_data_disk(p: Path) -> bool:
    """Create the box's /srv disk if missing; True when the guest may format
    it (never mounted before). An existing disk over the cap is refused."""
    from . import persist
    cap = max(64, int(settings.vm_svc_data_max_mb)) * 1024 * 1024
    if p.exists():
        info = json.loads(await persist._qemu_img("info", "--output=json", str(p)))
        if info.get("format") != "qcow2" or info.get("backing-filename"):
            raise ServiceError(f"{p.name} is not a plain qcow2; refusing it", 409)
        if int(info.get("virtual-size") or 0) > cap:
            raise ServiceError(f"{p.name} is larger than vm_svc_data_max_mb", 409)
        return not _ready_marker(p).exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    await persist._qemu_img("create", "-f", "qcow2", str(p), str(cap))
    p.chmod(0o600)
    return True


async def _delete_box_data(box: boxes.Box) -> None:
    """boxes.destroy(delete_data=True) from the VM manager."""
    if box.kind == "service":
        _delete_disk_file(box_disk(box))


# --- QMP (the box's own monitor socket) -------------------------------------------------

async def _qmp(box: boxes.Box, cmds: list[dict]) -> list[dict]:
    from . import persist
    return await persist.qmp(cmds, path=box.dir / "qmp.sock")


async def plug(box: boxes.Box, name: str, path: Path, *, read_only: bool) -> None:
    add, dev = await _qmp(box, [
        {"execute": "blockdev-add", "arguments": {
            "driver": "qcow2", "node-name": name, "read-only": read_only,
            "file": {"driver": "file", "node-name": f"{name}-file",
                     "filename": str(path), "read-only": read_only}}},
        {"execute": "device_add", "arguments": {
            "driver": "virtio-blk-pci", "id": f"{name}-dev", "drive": name,
            "bus": QMP_ROOT_PORT, "serial": name}}])
    if add.get("error") or dev.get("error"):
        if not add.get("error"):
            await _qmp(box, [{"execute": "blockdev-del",
                              "arguments": {"node-name": name}}])
        raise ServiceError(f"attach {name}: {add.get('error') or dev.get('error')}", 500)


async def unplug(box: boxes.Box, name: str, timeout: float = 15.0) -> None:
    await _qmp(box, [{"execute": "device_del", "arguments": {"id": f"{name}-dev"}}])
    deadline = time.monotonic() + timeout
    while True:
        (r,) = await _qmp(box, [{"execute": "blockdev-del",
                                 "arguments": {"node-name": name}}])
        if not r.get("error"):
            return
        if time.monotonic() > deadline:
            raise ServiceError(f"box {box.id} did not release {name}", 500)
        await asyncio.sleep(0.5)


# --- svcd RPC ---------------------------------------------------------------------------

async def svcd_rpc(box: boxes.Box, req: dict, timeout: float = _RPC_TIMEOUT) -> dict:
    """One NDJSON request/response to the box's svcd (host-dialed, port 5558).
    The reply is guest-authored: callers treat every field as a claim."""
    async def _go() -> dict:
        sock = await box.transport.connect(boxes.PORT_SVCD)
        reader, writer = await asyncio.open_connection(sock=sock, limit=2 ** 27)
        try:
            writer.write((json.dumps(req) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
            if not line:
                raise ConnectionError("svcd closed the connection")
            out = json.loads(line)
            if not isinstance(out, dict):
                raise ValueError("svcd reply is not an object")
            return out
        finally:
            writer.close()
    return await asyncio.wait_for(_go(), timeout)


async def open_tunnel(box: boxes.Box, port: int):
    """(reader, writer) of a byte stream to 127.0.0.1:<port> inside the box,
    through svcd (which only tunnels to ports the host exposed in its last
    apply). Used by portfwd's relays."""
    sock = await box.transport.connect(boxes.PORT_SVCD)
    reader, writer = await asyncio.open_connection(sock=sock)
    writer.write((json.dumps({"op": "tunnel", "port": int(port)}) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 10)
    try:
        ok = json.loads(line).get("ok") is True
    except (ValueError, AttributeError):
        ok = False
    if not ok:
        writer.close()
        raise ConnectionError(f"svcd refused the tunnel to {port}")
    return reader, writer


def unit_spec(row: dict, box: boxes.Box) -> dict:
    """What svcd needs to run one service (the artifact rides separately)."""
    return {"id": int(row["id"]), "name": row["name"],
            "command": list(row["command"]), "workdir": row["workdir"] or ".",
            "env": dict(row["env"]), "restart": row["restart"],
            "mem_mb": max(64, box.mem_mb - 128),
            "ports": [p["port"] for p in row["ports"]],
            "expose": [e["port"] for e in row["expose_ports"]],
            "artifact_sha256": row["artifact_sha256"]}


# --- /persist import ------------------------------------------------------------------------

def import_state_path(slug: str) -> Path:
    boxes._slug(slug)
    return svc_dir() / slug / "persist-import.json"


def import_tar_path(slug: str) -> Path:
    return svc_dir() / slug / "persist-import.tar"


def import_state(slug: str) -> dict | None:
    try:
        return json.loads(import_state_path(slug).read_text())
    except (OSError, ValueError, boxes.BoxError):
        return None


def _write_import_state(slug: str, st: dict) -> None:
    p = import_state_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st))


async def import_persist(slug: str, *, by: str = "operator") -> dict:
    """Operator decision 0.3, one click: freeze the project's old /persist
    (approval off, so it never attaches again), schedule its delete
    `persist_retire_days` from now, and copy its contents into the project's
    service box /srv (read-only at /persist for its services). The copy runs
    in a freshly booted service box before any service starts there: the old
    disk is plugged READ-ONLY at the QEMU block layer, svcd tars it back, the
    host keeps the tar (never extracts it) and pushes it to every service box
    of the project on each boot."""
    from . import persist
    from ..db import get_db
    if not settings.vm_boxes_enabled:
        raise ServiceError("import needs service boxes (vm_boxes_enabled)", 409)
    disk = persist.disk_path(slug)
    if not disk.exists():
        raise ServiceError(f"'{slug}' has no /persist disk to import", 404)
    if persist.holder() == slug:
        raise ServiceError("/persist is attached to a live turn; import once it "
                           "ends", 409)
    days = max(1, int(settings.persist_retire_days))
    db = await get_db()
    try:
        cur = await db.execute(
            "UPDATE projects SET persist_approved = 0, persist_imported_at ="
            " datetime('now'), persist_delete_after = datetime('now', ?)"
            " WHERE slug = ? AND deleted_at IS NULL", (f"+{days} days", slug))
        await db.commit()
        if cur.rowcount == 0:
            raise ServiceError("no such project", 404)
        async with db.execute("SELECT persist_imported_at, persist_delete_after "
                              "FROM projects WHERE slug = ?", (slug,)) as c:
            when = dict(await c.fetchone())
    finally:
        await db.close()
    _write_import_state(slug, {"state": "pending", "disk": str(disk), "by": by})
    await _event("persist_imported", project=slug,
                 summary=f"{by} imported /persist of '{slug}' into its service box "
                         f"/srv; the old disk is deleted after {when['persist_delete_after']}",
                 detail={"by": by, **when})
    _spawn(_run_import(slug))
    return {"slug": slug, "state": "pending", **when}


async def _run_import(slug: str) -> None:
    """Boot (or restart, for a fresh root) the project's per-project service
    box; _bring_up does the read phase before anything else runs there."""
    shape = {"id": 0, "project_slug": slug, "placement": "per_project"}
    try:
        box = boxes.get(box_id_of(shape)) or boxes.allocate(
            "service", project=slug, placement="per_project",
            variant=SVC_VARIANT, mem_mb=settings.vm_service_box_mem_mb)
        await _destroy_quiet(box, release=False)
        await _bring_up(box, [])
    except Exception as e:  # noqa: BLE001 — reported, retried by the next boot
        st = import_state(slug) or {}
        _write_import_state(slug, {**st, "error": f"{type(e).__name__}: {e}"[:300]})
        print(f"[services] import for {slug}: {e}")
    finally:
        _kick(box_id_of(shape))


async def _import_read(box: boxes.Box, slug: str) -> None:
    st = import_state(slug) or {}
    if st.get("state") != "pending":
        return
    disk = Path(st.get("disk") or "")
    if not disk.exists():
        _write_import_state(slug, {**st, "state": "failed", "error": "disk gone"})
        return
    await plug(box, _IMPORT_SERIAL, disk, read_only=True)
    try:
        cap = max(64, int(settings.vm_persist_max_mb)) * 1024 * 1024
        r = await svcd_rpc(box, {"op": "import_read", "max_bytes": cap},
                           _APPLY_TIMEOUT)
    finally:
        await unplug(box, _IMPORT_SERIAL)
    if not r.get("ok"):
        raise ServiceError(f"import read: {str(r.get('error'))[:200]}", 500)
    data = base64.b64decode(r.get("tar_b64") or "")
    if len(data) > cap:
        raise ServiceError("import: the guest returned more than the cap", 500)
    p = import_tar_path(slug)
    p.write_bytes(data)
    p.chmod(0o400)
    _write_import_state(slug, {**st, "state": "done", "bytes": len(data),
                               "sha256": hashlib.sha256(data).hexdigest(),
                               "error": None})


# --- the reconcile (host truth -> box) -------------------------------------------------------

_locks: dict[str, asyncio.Lock] = {}
_kicked: set[str] = set()
_tasks: set[asyncio.Task] = set()
_box_egress: dict[str, dict[str, list[int]]] = {}
_health: dict[str, dict] = {}      # box id -> {misses, restarts, next_restart, alerted}


def _spawn(coro) -> None:
    try:
        t = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _kick(box_id: str | None) -> None:
    """Reconcile a box soon (after the DB change that asked for it)."""
    if not box_id or not settings.vm_boxes_enabled or not _running:
        return
    _spawn(reconcile_box(box_id))


async def _wanted(box_id: str) -> list[dict]:
    return [r for r in await list_services(statuses=("approved",))
            if r["desired_state"] == "running" and box_id_of(r) == box_id]


async def _destroy_quiet(box: boxes.Box, *, release: bool) -> None:
    try:
        if release:
            await boxes.destroy(box)
        else:
            await boxes.stop(box)
    except Exception as e:  # noqa: BLE001
        print(f"[services] stop {box.id}: {e}")


async def reconcile_box(box_id: str) -> None:
    """Make box `box_id` match the approved, running definitions: none ->
    stopped and released; some -> booted fresh if down, then re-applied."""
    lock = _locks.setdefault(box_id, asyncio.Lock())
    async with lock:
        wanted = await _wanted(box_id)
        box = boxes.get(box_id)
        from . import portfwd
        if not wanted:
            _box_egress.pop(box_id, None)
            await portfwd.sync_box(box_id, [])
            if box is not None:
                await _destroy_quiet(box, release=True)
            return
        try:
            box = box or _box_for(wanted[0])
        except boxes.BoxError as e:
            for r in wanted:
                _set_runtime(r["id"], "failed", str(e))
            await _event("box_cap_refused", severity="info",
                         project=wanted[0]["project_slug"],
                         summary=f"service box {box_id} not started: {e}")
            return
        try:
            await _bring_up(box, wanted)
        except Exception as e:  # noqa: BLE001 — recorded; the pinger retries
            for r in wanted:
                _set_runtime(r["id"], "failed", f"{type(e).__name__}: {e}"[:300])
            print(f"[services] bring-up {box_id}: {e}")


async def _bring_up(box: boxes.Box, wanted: list[dict]) -> None:
    ctl = boxes.controller(box)
    if not ctl.running():
        # the root disk is a fresh overlay every boot: only /srv persists
        (box.dir / "overlay.qcow2").unlink(missing_ok=True)
        box.dir.mkdir(parents=True, exist_ok=True)
        await boxes.start(box)
        await _wait_svcd(box)
        if box.project:
            await _import_read(box, box.project)
        disk = box_disk(box)
        fresh = await ensure_data_disk(disk)
        await plug(box, _SRV_SERIAL, disk, read_only=False)
        r = await svcd_rpc(box, {"op": "mount_srv", "fresh": fresh}, _APPLY_TIMEOUT)
        if not r.get("ok"):
            raise ServiceError(f"mount /srv: {str(r.get('error'))[:200]}", 500)
        _ready_marker(disk).touch()
    await _apply(box, wanted)


async def _wait_svcd(box: boxes.Box, tries: int = 60) -> None:
    for _ in range(tries):
        try:
            r = await svcd_rpc(box, {"op": "ping"}, 5)
            if r.get("ok"):
                return
        except (OSError, ConnectionError, ValueError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(1)
    raise ServiceError(f"svcd in {box.id} never answered", 500)


async def _apply(box: boxes.Box, wanted: list[dict]) -> None:
    specs = []
    for r in wanted:
        spec = unit_spec(r, box)
        spec["artifact_b64"] = base64.b64encode(verify_artifact(r)).decode()
        specs.append(spec)
    req = {"op": "apply", "services": specs, "wipe": _pending_wipes(box_disk(box))}
    if box.project and (import_state(box.project) or {}).get("state") == "done":
        try:
            data = import_tar_path(box.project).read_bytes()
            req["import_tar_b64"] = base64.b64encode(data).decode()
            req["import_sha256"] = hashlib.sha256(data).hexdigest()
        except OSError:
            pass
    r = await svcd_rpc(box, req, _APPLY_TIMEOUT)
    if r.get("ok") and req["wipe"]:
        _wipe_path(box_disk(box)).unlink(missing_ok=True)
    errors = r.get("errors") if isinstance(r.get("errors"), dict) else {}
    for s in wanted:
        err = errors.get(str(s["id"]))
        _set_runtime(s["id"], "failed" if err else "running",
                     str(err)[:300] if err else None)
    eg: dict[str, list[int]] = {}
    for s in wanted:
        for h in s["egress_hosts"]:
            eg.setdefault(h, []).append(s["id"])
    _box_egress[box.id] = eg
    from . import portfwd
    await portfwd.sync_box(box.id, [(s["id"], e["port"], e["bind"])
                                    for s in wanted for e in s["expose_ports"]])


async def reconcile_all() -> None:
    ids = {box_id_of(r) for r in await list_services(statuses=("approved",))}
    ids |= {b.id for b in boxes.all_boxes() if b.kind == "service"}
    for bid in sorted(ids):
        await reconcile_box(bid)


# --- supervision (pings, svc_unreported, restart with backoff) ---------------------------------

async def _mark_reported(ids: list[int]) -> None:
    if not ids:
        return
    from ..db import get_db
    db = await get_db()
    try:
        await db.execute(
            f"UPDATE services SET last_reported_at = datetime('now') WHERE id IN "
            f"({','.join('?' * len(ids))})", ids)
        await db.commit()
    finally:
        await db.close()


def _unit_state(u) -> str:
    if not isinstance(u, dict):
        return "unreported"
    act = u.get("active")
    if act in ("active", "activating", "reloading"):
        return "running"
    if act == "failed" or u.get("result") not in (None, "", "success"):
        return "failed"
    return "stopped"


async def check_box(box_id: str) -> None:
    """One supervision pass over one service box."""
    wanted = await _wanted(box_id)
    box = boxes.get(box_id)
    if not wanted or box is None:
        return
    h = _health.setdefault(box_id, {"misses": 0, "restarts": 0,
                                    "next_restart": 0.0, "alerted": False})
    try:
        r = await svcd_rpc(box, {"op": "ping"}, 10)
        ok = r.get("ok") is True
    except (OSError, ConnectionError, ValueError, asyncio.TimeoutError):
        ok, r = False, {}
    if ok:
        units = r.get("units") if isinstance(r.get("units"), dict) else {}
        for s in wanted:
            st = _unit_state(units.get(str(s["id"])))
            _set_runtime(s["id"], st, None if st != "failed" else "unit failed")
        await _mark_reported([s["id"] for s in wanted])
        h.update(misses=0, alerted=False)
        return
    h["misses"] += 1
    if h["misses"] < _UNREPORTED_AFTER:
        return
    for s in wanted:
        _set_runtime(s["id"], "unreported", "svcd not answering")
    if not h["alerted"]:
        h["alerted"] = True
        await _event("svc_unreported", project=box.project,
                     summary=f"service box {box_id} stopped reporting "
                             f"({len(wanted)} service(s)); restarting it",
                     detail={"box_id": box_id, "services": [s["id"] for s in wanted]})
    now = time.monotonic()
    if now >= h["next_restart"]:
        delay = _RESTART_BACKOFF[min(h["restarts"], len(_RESTART_BACKOFF) - 1)]
        h["restarts"] += 1
        h["next_restart"] = now + delay
        await _destroy_quiet(box, release=False)     # kill QEMU; fresh root
        _spawn(reconcile_box(box_id))                # boot waits; don't hold the loop


async def on_svc_report(loop, conn, req: dict, box) -> None:
    """Gateway op `svc_report` (svcd's push). Identity is the CALLER's box
    (peer CID / listener), never anything in the message; it can only
    refresh last_reported_at for services that live in that box."""
    ok = box is not None and box.kind == "service"
    if ok:
        await _mark_reported([s["id"] for s in await _wanted(box.id)])
        _health.get(box.id, {}).update(misses=0)
    reply = {"type": "svc_report_ok" if ok else "error"}
    try:
        await loop.sock_sendall(conn, (json.dumps(reply) + "\n").encode())
    except OSError:
        pass


# --- egress (for the proxy / WP2) --------------------------------------------------------------

def _host_match(host: str, pats) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == p or host.endswith("." + p) for p in pats)


def egress_allowed_hosts(box_id: str) -> dict[str, list[int]]:
    """host -> service ids, for a service box's running services."""
    return dict(_box_egress.get(box_id, {}))


async def egress_decide(box_id: str, host: str) -> dict:
    """The verdict for one connection from a service box. Deny-by-default,
    whatever the profile says: allowed only if some running service in the
    box listed the host (exact or a subdomain of it), and neither the
    project's nor its profile's deny list names it. No auto-allow, no queue
    training. {allow, reason, service_ids}.

    ONE rule: this is a view over `egress.decide_service`, the function the
    proxy itself calls for service traffic, so the two can never disagree.
    `service_ids` (which running services in the box listed the host) is
    informational."""
    from .. import egress
    from ..db import get_db
    box = boxes.get(box_id)
    allowed = _box_egress.get(box_id) or {}
    sids = sorted({i for h, ids in allowed.items() if _host_match(host, [h]) for i in ids})
    if box is None or box.kind != "service":
        return {"allow": False, "reason": "not a service box", "service_ids": []}
    db = await get_db()
    try:
        verdict, reason = await egress.decide_service(db, box.project, box.service_id, host)
    finally:
        await db.close()
    return {"allow": verdict == "allow", "reason": reason, "service_ids": sids}


# --- logs --------------------------------------------------------------------------------

async def logs(sid: int, lines: int = 100) -> str:
    """The unit's journal tail from svcd, secret-scrubbed and fenced. This is
    service output: untrusted (service_logs is in broker._UNTRUSTED_TOOLS)."""
    row = await get(sid)
    if row is None:
        raise ServiceError("no such service", 404)
    if row["status"] != "approved":
        raise ServiceError(f"service #{sid} is {row['status']}; no logs", 409)
    box = boxes.get(box_id_of(row))
    if box is None:
        raise ServiceError("its box is not running", 409)
    lines = max(1, min(int(lines or 100), MAX_LOG_LINES))
    try:
        r = await svcd_rpc(box, {"op": "logs", "id": sid, "lines": lines}, 15)
    except (OSError, ConnectionError, ValueError, asyncio.TimeoutError) as e:
        raise ServiceError(f"svcd unreachable: {type(e).__name__}", 502) from None
    from .. import secrets
    text = str(r.get("text") or "")[:_LOG_CAP]
    return secrets.scrub(text) or ""


# --- the process (start/stop, registration) ------------------------------------------------------

_running = False
_loop_task: asyncio.Task | None = None


def register() -> None:
    """Hook into the box runtime (idempotent; import-time safe)."""
    if getattr(register, "_done", False):
        return
    register._done = True
    boxes.add_data_deleter(_delete_box_data)
    from . import gateway_server, svc_pkg
    reg_pkg = getattr(gateway_server, "register_package_builder", None)
    if reg_pkg:
        reg_pkg("service", svc_pkg.build_for_box)
    reg_op = getattr(gateway_server, "register_op_handler", None)
    if reg_op:
        reg_op("svc_report", on_svc_report)


async def _supervise() -> None:
    last_sweep = 0.0
    while True:
        try:
            if time.monotonic() - last_sweep > 3600:
                last_sweep = time.monotonic()
                from . import persist
                await persist.sweep_retired()
            if settings.vm_boxes_enabled:
                for b in [b for b in boxes.all_boxes() if b.kind == "service"]:
                    await check_box(b.id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — supervision must keep running
            print(f"[services] supervise: {e}")
        await asyncio.sleep(max(2, int(settings.vm_svc_ping_seconds)))


async def start() -> None:
    """App start: register, boot every box with approved running services."""
    global _running, _loop_task
    register()
    _running = True
    if settings.vm_boxes_enabled:
        _spawn(reconcile_all())
    _loop_task = asyncio.get_running_loop().create_task(_supervise())


async def stop() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
    from . import portfwd
    await portfwd.close_all()
    for b in [b for b in boxes.all_boxes() if b.kind == "service"]:
        await _destroy_quiet(b, release=False)


def _reset_for_tests() -> None:
    _runtime.clear()
    _box_egress.clear()
    _health.clear()
    _locks.clear()


# import-time registration (like images.py): the gateway serves svcd to a
# service box and routes svc_report even before the lifespan start() runs
register()
