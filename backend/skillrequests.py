"""Skill install requests: an agent names an existing skill, the operator
fetches it host-side, reads it and installs + grants it
(docs/agent-requests-contract.md, section 2).

file_request never touches the network: an agent-chosen URL is contacted only
when the operator presses Fetch, because an agent-triggered host fetch would
bypass the egress proxy. Approval installs exactly the staged bytes the
operator read (the SKILL.md hash must match and every file is re-hashed)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from . import security, skillimport
from .agent.tools import imported
from .agent.tools.registry import _parse_md, compile_registry
from .config import settings

MAX_PENDING_PER_PROJECT = 5
SOURCE_MAX = 500
REASON_MAX = 1000
SKILL_MD_CAP = 20_000

_BAD_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")
_JSON_COLS = {"files": {}, "flags": [], "requirements": []}


class SkillRequestError(Exception):
    """Refused, with a reason the operator or agent can act on."""


# ---------------------------------------------------------------------------
# Validation (no network)
# ---------------------------------------------------------------------------
def check_source(source: str) -> str:
    if not isinstance(source, str) or not source:
        raise SkillRequestError("give a source: an https git URL or clawhub:<slug>")
    if len(source) > SOURCE_MAX:
        raise SkillRequestError(f"source is longer than {SOURCE_MAX} characters")
    if _BAD_CHARS.search(source):
        raise SkillRequestError("source must not contain whitespace or control characters")
    if source.startswith("clawhub:"):
        if not skillimport._CLAWHUB_RE.match(source[len("clawhub:"):]):
            raise SkillRequestError("bad ClawHub reference; use clawhub:<slug>[@version]")
        return source
    if not source.startswith("https://"):
        raise SkillRequestError("source must be an https:// git URL or clawhub:<slug>")
    url, _, subdir = source.partition("#")
    rest = url[len("https://"):]
    if "?" in url:
        raise SkillRequestError("a query string is not allowed in the source")
    host = rest.partition("/")[0]
    if not host or "@" in rest:
        raise SkillRequestError("credentials (user@host) are not allowed in the source")
    if not re.match(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$", host):
        raise SkillRequestError("bad host in the source URL")
    if "#" in source:
        sub = subdir.strip("/")
        if not sub or not skillimport._SUBDIR_RE.match(sub) or ".." in sub.split("/"):
            raise SkillRequestError("bad sub-directory after #")
    return source


def _check_name(name: str | None) -> str | None:
    if name is None or name == "":
        return None
    if not isinstance(name, str) or not imported.NAME_RE.match(name):
        raise SkillRequestError("name must be 1-64 letters, digits, _ or -")
    return name


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
def staging_dir(req_id: int) -> Path:
    return settings.data_dir / "skill_staging" / str(req_id)


def _row(r) -> dict:
    d = dict(r)
    for col, empty in _JSON_COLS.items():
        try:
            d[col] = json.loads(d[col]) if d.get(col) else empty
        except ValueError:
            d[col] = empty
    d["granted"] = bool(d.get("granted"))
    d["skill_md"] = None
    if d["status"] == "fetched":
        try:
            d["skill_md"] = (staging_dir(d["id"]) / "SKILL.md").read_text(
                encoding="utf-8")[:SKILL_MD_CAP]
        except OSError:
            pass
    return d


async def get(db, req_id: int) -> dict | None:
    async with db.execute("SELECT * FROM skill_requests WHERE id = ?", (req_id,)) as cur:
        r = await cur.fetchone()
    return _row(r) if r else None


async def list_rows(db, status: str | None = None, project: str | None = None) -> list[dict]:
    sql, args, where = "SELECT * FROM skill_requests", [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if project:
        where.append("project_slug = ?")
        args.append(project)
    if where:
        sql += " WHERE " + " AND ".join(where)
    async with db.execute(sql + " ORDER BY id DESC", args) as cur:
        return [_row(r) for r in await cur.fetchall()]


def _rmstaging(req_id: int) -> None:
    shutil.rmtree(staging_dir(req_id), ignore_errors=True)


# ---------------------------------------------------------------------------
# Agent: file a request
# ---------------------------------------------------------------------------
async def file_request(db, *, source: str, name: str | None, reason: str,
                       project: str | None, conversation_id: int | None) -> dict:
    source = check_source(source.strip() if isinstance(source, str) else source)
    name = _check_name(name)
    reason = (reason or "").strip()
    if not reason:
        raise SkillRequestError("say what the skill is for")
    reason = reason[:REASON_MAX]
    async with db.execute(
            "SELECT 1 FROM skill_requests WHERE source = ? "
            "AND status IN ('pending', 'fetched')", (source,)) as cur:
        if await cur.fetchone():
            raise SkillRequestError("that source is already requested and waiting "
                                    "for the operator")
    async with db.execute(
            "SELECT COUNT(*) FROM skill_requests WHERE status IN ('pending', 'fetched') "
            "AND COALESCE(project_slug, '') = ?", (project or "",)) as cur:
        if (await cur.fetchone())[0] >= MAX_PENDING_PER_PROJECT:
            raise SkillRequestError(f"{MAX_PENDING_PER_PROJECT} skill requests are "
                                    "already waiting for the operator")
    cur = await db.execute(
        "INSERT INTO skill_requests(project_slug, conversation_id, source, name, reason) "
        "VALUES (?,?,?,?,?)", (project, conversation_id, source, name, reason))
    await db.commit()
    row = await get(db, cur.lastrowid)
    await security.raise_event(
        db, kind="skill_requested", severity="info", project=project,
        summary=f"agent requested skill {source}",
        detail={"request_id": row["id"], "source": source, "name": name,
                "reason": reason, "conversation_id": conversation_id})
    return row


# ---------------------------------------------------------------------------
# Operator: fetch, approve, reject
# ---------------------------------------------------------------------------
def _fetch_sync(source: str, stage: Path) -> dict:
    """Run the importer's fetch + vetting into a temp dir, then copy the vetted
    bytes to `stage` (replacing an earlier fetch)."""
    with tempfile.TemporaryDirectory(prefix="jav3-skillreq-") as tmp:
        src, prov = skillimport._fetch(source, Path(tmp))
        if not src.is_dir():
            raise skillimport.SkillImportError("that source has no such folder")
        files = skillimport._collect(src)
        try:
            meta = _parse_md(src / "SKILL.md")
        except Exception:  # noqa: BLE001 — malformed YAML is the author's problem
            meta = None
        if meta is None:
            raise skillimport.SkillImportError(
                "SKILL.md needs frontmatter with name and description")
        flags = skillimport.scan(files)
        reqs = imported.requirements(meta)
    shutil.rmtree(stage, ignore_errors=True)
    for rel, data in files.items():
        out = stage / rel
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        out.write_bytes(data)
        out.chmod(0o644)
    return {"ref": prov.get("ref", ""), "skill_name": str(meta.get("name") or ""),
            "description": imported.sanitize_description(meta.get("description", "")),
            "skill_sha256": hashlib.sha256(files["SKILL.md"]).hexdigest(),
            "files": {rel: hashlib.sha256(d).hexdigest() for rel, d in sorted(files.items())},
            "flags": flags,
            "requirements": [{"reason": r.get("reason", ""), "met": bool(r.get("met"))}
                             for r in reqs]}


async def fetch(db, req_id: int) -> dict:
    row = await get(db, req_id)
    if row is None:
        raise LookupError("no such skill request")
    if row["status"] not in ("pending", "fetched"):
        raise SkillRequestError(f"request is {row['status']}; only pending or "
                                "fetched requests can be fetched")
    try:
        res = await asyncio.to_thread(_fetch_sync, row["source"], staging_dir(req_id))
    except skillimport.SkillImportError as e:
        await db.execute("UPDATE skill_requests SET error = ? WHERE id = ?",
                         (str(e)[:500], req_id))
        await db.commit()
        raise
    await db.execute(
        "UPDATE skill_requests SET status = 'fetched', fetched_at = datetime('now'), "
        "ref = ?, skill_name = ?, description = ?, skill_sha256 = ?, files = ?, "
        "flags = ?, requirements = ?, error = NULL WHERE id = ?",
        (res["ref"], res["skill_name"], res["description"], res["skill_sha256"],
         json.dumps(res["files"]), json.dumps(res["flags"]),
         json.dumps(res["requirements"]), req_id))
    await db.commit()
    return await get(db, req_id)


def _verify_staging(req_id: int, files: dict) -> None:
    base = staging_dir(req_id)
    found: dict[str, Path] = {}
    if base.is_dir():
        for p in base.rglob("*"):
            if p.is_symlink():
                raise SkillRequestError(f"staged copy changed: symlink {p.name}")
            if p.is_file():
                found[p.relative_to(base).as_posix()] = p
    if set(found) != set(files):
        raise SkillRequestError("staged copy changed since it was fetched; fetch again")
    for rel, p in found.items():
        if imported.sha256_file(p) != files[rel]:
            raise SkillRequestError(f"staged {rel} changed since it was fetched; "
                                    "fetch again")


async def approve(db, req_id: int, *, skill_sha256: str, decided_by: str,
                  by_operator: bool) -> dict:
    row = await get(db, req_id)
    if row is None:
        raise LookupError("no such skill request")
    if row["status"] != "fetched":
        raise SkillRequestError(f"request is {row['status']}; fetch it first")
    if not skill_sha256 or skill_sha256 != row["skill_sha256"]:
        raise SkillRequestError("that is not the SKILL.md you were shown; reload and "
                                "read it again")
    _verify_staging(req_id, row["files"])
    try:
        info = await asyncio.to_thread(
            skillimport.vendor, staging_dir(req_id),
            {"source": row["source"], "ref": row["ref"] or ""}, row["name"] or None)
    except skillimport.SkillImportError as e:
        raise SkillRequestError(str(e)) from None
    dest = settings.skills_dir / info["slug"]
    pin = imported.read_pin(dest) or {}
    if pin.get("files") != row["files"]:      # bytes moved between check and copy
        shutil.rmtree(dest, ignore_errors=True)
        compile_registry()
        raise SkillRequestError("staged copy changed during install; nothing installed")
    unmet = [r["reason"] for r in info["requirements"] if not r["met"]]
    granted, error = 0, None
    if info["blocked"]:
        error = f"installed but not granted: {info['blocked']}"
    elif unmet:
        error = f"installed but not granted: {unmet[0]}"
    else:
        imported.set_grant(dest, True)
        compile_registry()
        granted = 1
    await db.execute(
        "UPDATE skill_requests SET status = 'installed', installed_slug = ?, "
        "granted = ?, error = ?, decided_by = ?, decided_at = datetime('now') "
        "WHERE id = ?", (info["slug"], granted, error, decided_by, req_id))
    await db.commit()
    _rmstaging(req_id)
    await security.raise_event(
        db, kind="skill_installed", severity="warn", project=row["project_slug"],
        summary=f"skill {info['name']} installed from {row['source']}"
                f"{'' if granted else ' (not granted)'}",
        detail={"request_id": req_id, "source": row["source"], "ref": row["ref"],
                "slug": info["slug"], "granted": bool(granted),
                "flags": row["flags"][:20], "skill_sha256": row["skill_sha256"]},
        actor=security.OPERATOR if by_operator else None)
    return await get(db, req_id)


async def reject(db, req_id: int, *, reason: str, decided_by: str,
                 by_operator: bool) -> dict:
    row = await get(db, req_id)
    if row is None:
        raise LookupError("no such skill request")
    if row["status"] not in ("pending", "fetched"):
        raise SkillRequestError(f"request is {row['status']}; it cannot be rejected")
    await db.execute(
        "UPDATE skill_requests SET status = 'rejected', error = ?, decided_by = ?, "
        "decided_at = datetime('now') WHERE id = ?",
        ((reason or "")[:500] or None, decided_by, req_id))
    await db.commit()
    _rmstaging(req_id)
    await security.raise_event(
        db, kind="skill_request_rejected", severity="info", project=row["project_slug"],
        summary=f"skill request {row['source']} rejected",
        detail={"request_id": req_id, "source": row["source"], "reason": reason},
        actor=security.OPERATOR if by_operator else None)
    return await get(db, req_id)
