"""Import an OpenClaw skill folder as a pinned, ungranted, untrusted snapshot.

Sources (all fetched HOST-side; the guest never fetches a skill):
  a local folder         /path/to/skill            (the folder holding SKILL.md)
  a git URL              https://host/owner/repo.git[#sub/dir]   shallow clone
  ClawHub                clawhub:<slug>[@version]  only with JARVIS_SKILL_IMPORT_CLAWHUB

The copy lands in skills/oc-<name>/ with a .pin.json (source, ref, sha256 of
every file, import time). What the importer never does: run an install hint,
keep an executable bit, follow a symlink, accept a binary, or update later —
a newer version is a deliberate re-import, which re-pins and revokes the grant.
The text is run through diffgate.scan as an ADVISORY tripwire (flags land as a
security event); the operator's review before granting is the real gate.

See backend/agent/tools/imported.py for what the registry does with the pin.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import diffgate, websec
from .agent.tools import imported
from .agent.tools.registry import _parse_md, compile_registry, load_registry
from .config import settings

MAX_FILES = 64
MAX_TOTAL_BYTES = 256 * 1024
MAX_DOWNLOAD_BYTES = 1024 * 1024
GIT_TIMEOUT = 120
_SUBDIR_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_CLAWHUB_RE = re.compile(r"^([a-z0-9][a-z0-9._-]{0,80}(?:/[a-z0-9][a-z0-9._-]{0,80})?)"
                         r"(?:@([0-9A-Za-z.+-]{1,40}))?$")


class SkillImportError(Exception):
    """Refused, with a reason the operator can act on."""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def _allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in settings.skill_import_allow_hosts.split(",")
            if h.strip()}


def _check_url(url: str) -> None:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host in _allowed_hosts() and p.scheme in ("http", "https"):
        return
    if p.scheme != "https":
        raise SkillImportError("git URLs must be https:// (or a host listed in "
                               "JARVIS_SKILL_IMPORT_ALLOW_HOSTS)")
    try:
        websec.is_safe_url(url)
    except websec.UnsafeURL as e:
        raise SkillImportError(f"refused: {e}") from None


def _git(args: list[str], cwd: Path | None = None) -> str:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(cwd or tempfile.gettempdir()),
           "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ALLOW_PROTOCOL": "http:https"}
    # no redirects (each hop would dodge the SSRF check), no symlinks checked
    # out, no submodules, no credential prompts, no hooks config from anywhere
    base = ["git", "-c", "http.followRedirects=false", "-c", "core.symlinks=false",
            "-c", "core.hooksPath=/dev/null", "-c", "credential.helper="]
    r = subprocess.run(base + args, cwd=cwd, env=env, capture_output=True,
                       text=True, timeout=GIT_TIMEOUT)
    if r.returncode != 0:
        raise SkillImportError(f"git {args[0]} failed: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def _clone(url: str, subdir: str, dest: Path) -> str:
    """Shallow-clone url into dest (sparse to `subdir` when given); returns
    the commit id."""
    args = ["clone", "--depth", "1", "--no-tags", "--single-branch",
            "--no-recurse-submodules"]
    if subdir:
        args += ["--filter=blob:none", "--sparse"]
    _git(args + ["--", url, str(dest)])
    if subdir:
        _git(["sparse-checkout", "set", "--", subdir], cwd=dest)
    return _git(["rev-parse", "HEAD"], cwd=dest)


def _fetch_git(source: str, tmp: Path) -> tuple[Path, dict]:
    url, _, subdir = source.partition("#")
    subdir = subdir.strip("/")
    if subdir and (not _SUBDIR_RE.match(subdir) or ".." in subdir.split("/")):
        raise SkillImportError(f"bad sub-directory '{subdir}'")
    _check_url(url)
    dest = tmp / "repo"
    ref = _clone(url, subdir, dest)
    shutil.rmtree(dest / ".git", ignore_errors=True)
    return (dest / subdir if subdir else dest), {"source": source, "ref": ref}


def _safe_client() -> httpx.Client:
    def check(request: httpx.Request) -> None:
        try:
            websec.is_safe_url(str(request.url))
        except websec.UnsafeURL as e:
            raise SkillImportError(f"refused: {e}") from None
    return httpx.Client(timeout=30, follow_redirects=True,
                        event_hooks={"request": [check]})


def _extract_zip(data: bytes, dest: Path) -> None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillImportError("ClawHub did not return a zip") from None
    total = 0
    for info in zf.infolist():
        name = info.filename
        parts = Path(name).parts
        if name.startswith("/") or ".." in parts or ":" in name:
            raise SkillImportError(f"unsafe path in archive: {name}")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise SkillImportError(f"symlink in archive: {name}")
        if info.is_dir():
            continue
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise SkillImportError("archive too large")
        out = dest / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zf.read(info))


def _fetch_clawhub(spec: str, tmp: Path) -> tuple[Path, dict]:
    if not settings.skill_import_clawhub:
        raise SkillImportError("ClawHub import is off (JARVIS_SKILL_IMPORT_CLAWHUB). "
                               "Prefer a vetted git repo or folder.")
    m = _CLAWHUB_RE.match(spec)
    if not m:
        raise SkillImportError(f"bad ClawHub slug '{spec}'")
    slug, version = m.group(1), m.group(2)
    base = settings.skill_import_clawhub_url.rstrip("/")
    with _safe_client() as c:
        r = c.get(f"{base}/api/v1/skills/{slug}")
        if r.status_code != 200:
            raise SkillImportError(f"ClawHub has no skill '{slug}' ({r.status_code})")
        info = r.json()
        mod = info.get("moderation") or {}
        if mod.get("isSuspicious") or mod.get("isMalwareBlocked") or \
                mod.get("verdict") not in (None, "clean"):
            raise SkillImportError(f"ClawHub flags '{slug}' as "
                                   f"{mod.get('verdict') or 'suspicious'} — refused")
        version = version or ((info.get("latestVersion") or {}).get("version"))
        params = {"slug": slug, **({"version": version} if version else {})}
        r = c.get(f"{base}/api/v1/download", params=params)
        if r.status_code != 200:
            raise SkillImportError(f"ClawHub download failed ({r.status_code})")
        if len(r.content) > MAX_DOWNLOAD_BYTES:
            raise SkillImportError("ClawHub download too large")
        if "json" in r.headers.get("content-type", ""):
            j = r.json()
            raise SkillImportError(
                "this ClawHub skill lives on GitHub — import it as "
                f"https://github.com/{j.get('repo', '?')}#{j.get('path', '')}")
    dest = tmp / "zip"
    _extract_zip(r.content, dest)
    if not (dest / "SKILL.md").is_file():
        tops = [p for p in dest.iterdir() if (p / "SKILL.md").is_file()]
        if len(tops) != 1:
            raise SkillImportError("archive has no SKILL.md")
        dest = tops[0]
    return dest, {"source": f"clawhub:{slug}", "ref": version or ""}


def _fetch(source: str, tmp: Path) -> tuple[Path, dict]:
    source = source.strip()
    if source.startswith("clawhub:"):
        return _fetch_clawhub(source[len("clawhub:"):], tmp)
    if re.match(r"^[a-z][a-z0-9+.-]*://", source, re.I) or source.startswith("git@"):
        if not source.lower().startswith(("https://", "http://")):
            raise SkillImportError("only https:// git URLs are accepted")
        return _fetch_git(source, tmp)
    path = Path(source).expanduser()
    if not path.is_dir():
        raise SkillImportError(f"no such folder: {source}")
    return path, {"source": str(path.resolve()), "ref": ""}


# ---------------------------------------------------------------------------
# Vet + vendor
# ---------------------------------------------------------------------------
def _collect(src: Path) -> dict[str, bytes]:
    """Every file under src as bytes, refusing anything a skill should not be."""
    files: dict[str, bytes] = {}
    total = 0
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src).as_posix()
        if rel == ".git" or rel.startswith(".git/") or rel == imported.PIN_FILE:
            continue
        if p.is_symlink():
            raise SkillImportError(f"symlink refused: {rel}")
        if p.is_dir():
            continue
        if not p.is_file():
            raise SkillImportError(f"not a regular file: {rel}")
        data = p.read_bytes()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise SkillImportError(f"not UTF-8 text: {rel}") from None
        total += len(data)
        files[rel] = data
        if len(files) > MAX_FILES:
            raise SkillImportError(f"more than {MAX_FILES} files")
        if total > MAX_TOTAL_BYTES:
            raise SkillImportError(f"larger than {MAX_TOTAL_BYTES // 1024} KiB")
    if "SKILL.md" not in files:
        raise SkillImportError("no SKILL.md at the top of the folder")
    return files


_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.S | re.M)


def scan(files: dict[str, bytes]) -> list[dict]:
    """diffgate over the bundle. Markdown is not a code type diffgate reads, so
    a .md file's fenced blocks are scanned as shell — that is what a skill's
    code blocks become when the model follows them."""
    flags: list[dict] = []
    for rel, data in files.items():
        text = data.decode("utf-8")
        if rel.lower().endswith(".md"):
            chunks = [(f"{rel}#block{i + 1}.sh", b) for i, b in enumerate(_FENCE.findall(text))]
        else:
            chunks = [(rel, text)]
        for path, chunk in chunks:
            for f in diffgate.scan("", chunk, path):
                flags.append({"file": path, "trigger": f["trigger"],
                              "detail": {k: v for k, v in f["detail"].items()
                                         if k != "lines"}})
    return flags


def _dir_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def vendor(src: Path, provenance: dict, name: str | None = None,
           replace: bool = False) -> dict:
    files = _collect(src)
    try:
        meta = _parse_md(src / "SKILL.md")
    except Exception:  # noqa: BLE001 — malformed YAML is the author's problem
        meta = None
    if meta is None:
        raise SkillImportError("SKILL.md needs frontmatter with name and description")
    reg_name = (name or str(meta["name"])).strip()
    if not imported.NAME_RE.match(reg_name):
        raise SkillImportError(f"'{reg_name}' is not a valid tool name; pass --name")
    slug = _dir_slug(reg_name)
    dest = settings.skills_dir / f"{imported.IMPORT_PREFIX}{slug}"
    if dest.exists() and not replace:
        raise SkillImportError(f"{dest.name} is already imported (re-import to update)")
    taken = {e["name"]: e for e in load_registry()
             if Path(e["source"]).parent != dest and not e.get("clash")}
    if reg_name in taken:
        raise SkillImportError(f"the name '{reg_name}' is taken by the "
                               f"{taken[reg_name]['kind']} '{reg_name}'; pass --name")

    flags = scan(files)
    if dest.exists():
        shutil.rmtree(dest)
    for rel, data in files.items():
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        out.write_bytes(data)
        out.chmod(0o644)                     # never an executable bit
    pin = {"origin": imported.ORIGIN, "name": reg_name,
           "source": provenance.get("source", ""), "ref": provenance.get("ref", ""),
           "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "files": {rel: imported.sha256_file(dest / rel) for rel in sorted(files)},
           "scan": flags}
    (dest / imported.PIN_FILE).write_text(json.dumps(pin, indent=2))
    imported.set_grant(dest, False)          # a (re-)import is never granted
    if flags:
        imported.alert("skill_import_flag", f"import:{dest.name}:{pin['imported_at']}",
                       f"Imported skill {dest.name}: {len(flags)} diff-gate flag(s) "
                       "— read it before granting",
                       {"skill": dest.name, "source": pin["source"],
                        "flags": flags[:20]})
    entries = compile_registry()
    e = next(x for x in entries if Path(x["source"]).parent == dest)
    return {"slug": dest.name, "name": reg_name, "source": pin["source"],
            "ref": pin["ref"], "files": len(files), "flags": flags,
            "requirements": e.get("requirements", []),
            "install_hints": e.get("install_hints", []),
            "blocked": e.get("blocked", "") or e.get("clash", "")}


def import_skill(source: str, name: str | None = None, replace: bool = False) -> dict:
    if not source or not source.strip():
        raise SkillImportError("give a folder, a git URL or clawhub:<slug>")
    with tempfile.TemporaryDirectory(prefix="jav3-skill-") as tmp:
        src, prov = _fetch(source, Path(tmp))
        if not src.is_dir():
            raise SkillImportError("that source has no such folder")
        return vendor(src, prov, name=name, replace=replace)
