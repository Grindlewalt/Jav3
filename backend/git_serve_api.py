"""Read-only git smart-HTTP: serve each project's own repo so the operator can

    git clone http://<jav3-host>/git/<slug>
    git pull   (thereafter)

over the LAN — no Gitea, no tokens, no external server. Every project is already
a git repo on disk (`projects/<slug>`, git-initialised at creation), so this just
speaks git's HTTP transport against it.

Pull-only by design: only `git-upload-pack` (clone/fetch) is served; a push
(`git-receive-pack`) is refused. Projects are written THROUGH Jav3, not by
pushing git — so nothing here needs write access, and the attack surface stays a
read of already-committed content. Gated by HTTP Basic against the same user
store as the rest of the app (git prompts once and caches), so project source
isn't left open on the LAN. Served content is the last COMMITTED state (the
agent's uncommitted working changes land only once a commit request is approved).
"""
import asyncio
import base64
import gzip
import os
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from .auth import verify_password
from .config import settings
from .db import get_db

router = APIRouter(prefix="/git", tags=["git-serve"])

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")   # slugify() output; no path tricks
GIT_TIMEOUT = 120
_UPLOAD_PACK = "git-upload-pack"


def _guard_enabled() -> None:
    if not settings.git_serve_enabled:
        raise HTTPException(status_code=404, detail="not found")


def _repo_dir(slug: str):
    """The project's repo dir, or 404. The slug regex plus the fixed base dir
    means no '..'/absolute path can escape settings.projects_dir."""
    if not _SLUG.match(slug or ""):
        raise HTTPException(status_code=404, detail="no such project")
    d = settings.projects_dir / slug
    if not d.is_dir() or not (d / "project.md").exists() or not (d / ".git").exists():
        raise HTTPException(status_code=404, detail="no such project")
    return d


_UNAUTH = HTTPException(status_code=401, detail="authentication required",
                        headers={"WWW-Authenticate": 'Basic realm="Jav3 git"'})


async def _require_basic(request: Request) -> None:
    """HTTP Basic against the app's users table (same creds as the GUI login)."""
    hdr = request.headers.get("authorization", "")
    if not hdr.startswith("Basic "):
        raise _UNAUTH
    try:
        user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
    except Exception:  # noqa: BLE001 — malformed header == no credential
        raise _UNAUTH
    if not user or not pw:
        raise _UNAUTH
    db = await get_db()
    try:
        async with db.execute(
                "SELECT password_hash FROM users WHERE username = ?", (user,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    if not row or not verify_password(pw, row["password_hash"]):
        raise _UNAUTH


def _git_env(request: Request) -> dict:
    """A clean env for git, forwarding the client's protocol version (git
    defaults to v2, which upload-pack only honours when GIT_PROTOCOL is set)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    proto = request.headers.get("git-protocol")
    if proto:
        env["GIT_PROTOCOL"] = proto
    return env


def _pkt(payload: bytes) -> bytes:
    """One git pkt-line: 4-hex length prefix (inclusive) + payload."""
    return ("%04x" % (len(payload) + 4)).encode() + payload


@router.get("", response_class=PlainTextResponse)
@router.get("/", response_class=PlainTextResponse)
async def index(request: Request) -> str:
    """A human-readable list of pullable projects with ready-to-copy commands."""
    _guard_enabled()
    await _require_basic(request)
    base = str(request.base_url).rstrip("/")
    lines = ["Jav3 projects available over git (read-only):", ""]
    slugs = []
    if settings.projects_dir.is_dir():
        for d in sorted(settings.projects_dir.iterdir()):
            if (d / "project.md").exists() and (d / ".git").exists() \
                    and _SLUG.match(d.name):
                slugs.append(d.name)
    for slug in slugs:
        lines.append(f"  git clone {base}/git/{slug}")
    if not slugs:
        lines.append("  (no projects yet)")
    return "\n".join(lines) + "\n"


@router.get("/{slug}/info/refs")
async def info_refs(slug: str, service: str, request: Request):
    _guard_enabled()
    await _require_basic(request)
    if service != _UPLOAD_PACK:
        # receive-pack (push) or anything else: this surface is pull-only.
        raise HTTPException(
            status_code=403,
            detail="read-only: only clone/fetch (git-upload-pack) is served here")
    repo = _repo_dir(slug)
    proc = await asyncio.create_subprocess_exec(
        "git", "upload-pack", "--stateless-rpc", "--advertise-refs", str(repo),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_git_env(request))
    try:
        out, err = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise HTTPException(status_code=504, detail="git upload-pack timed out")
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail="git upload-pack failed")
    body = _pkt(b"# service=git-upload-pack\n") + b"0000" + out
    return Response(
        content=body,
        media_type="application/x-git-upload-pack-advertisement",
        headers={"Cache-Control": "no-cache, max-age=0, must-revalidate"})


@router.post("/{slug}/git-upload-pack")
async def upload_pack(slug: str, request: Request):
    _guard_enabled()
    await _require_basic(request)
    repo = _repo_dir(slug)
    body = await request.body()
    if request.headers.get("content-encoding", "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError:
            raise HTTPException(status_code=400, detail="bad gzip body")
    proc = await asyncio.create_subprocess_exec(
        "git", "upload-pack", "--stateless-rpc", str(repo),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=_git_env(request))
    try:
        out, err = await asyncio.wait_for(proc.communicate(body), GIT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise HTTPException(status_code=504, detail="git upload-pack timed out")
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail="git upload-pack failed")
    return Response(content=out,
                    media_type="application/x-git-upload-pack-result",
                    headers={"Cache-Control": "no-cache, max-age=0, must-revalidate"})
