"""Git gate: the agent can only *request* a commit; the host commits (and
pushes, when a remote is configured) after operator approval via the API.

The repo is the project dir itself. Staging metadata and runtime files are
kept out via a host-written .gitignore, and the agent can never write into
.git/ (writes + workspace endpoints refuse the path).
"""
import asyncio
import base64
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings
from .db import get_db

GIT_TIMEOUT = 30
NET_TIMEOUT = 120       # push / fetch / ls-remote cross the internet on a Pi
CLONE_TIMEOUT = 300

GITIGNORE = ".staging/\n.workspace.json\n.context.json\ndata/\n"


def _project_dir(slug: str) -> Path:
    return settings.projects_dir / slug


async def run_git(slug: str, *args: str, check: bool = False,
                  extra_env: dict[str, str] | None = None,
                  timeout: int = GIT_TIMEOUT) -> tuple[int, str, str]:
    """git -C <project dir> <args>. Never a shell; env stripped of GIT_* surprises."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra_env:
        env.update(extra_env)
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(_project_dir(slug)), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s")
    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {_scrub((err or out).strip())}")
    return proc.returncode, out, err


# per-slug init lock: git_status/git_diff are read_only-flagged, so the loop
# may run them concurrently — two first-touches must not race `git init`
_repo_locks: dict[str, asyncio.Lock] = {}


async def ensure_repo(slug: str) -> None:
    lock = _repo_locks.setdefault(slug, asyncio.Lock())
    async with lock:
        d = _project_dir(slug)
        if not (d / ".git").exists():
            await run_git(slug, "init", "-q", check=True)
            await run_git(slug, "config", "user.name", "Jarvis", check=True)
            await run_git(slug, "config", "user.email", "jarvis@atomos.local", check=True)
        gitignore = d / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(GITIGNORE)


# --- GitHub remote plumbing --------------------------------------------------
# The remote URL is stored CLEAN in projects.github_remote and .git/config
# ("origin"). Auth rides only in per-invocation env (GIT_CONFIG_* ->
# http.extraheader), so the token never appears in argv, .git/config, the DB,
# or persisted error text; _scrub is belt-and-braces on every surfaced string.
# All of this is operator-only surface — no agent tool can set a remote,
# push, or pull.

def github_token() -> str | None:
    from . import secrets as secrets_store
    return (secrets_store.load().get("GITHUB_TOKEN")
            or os.environ.get("JARVIS_GITHUB_TOKEN") or None)


def valid_remote(url: str) -> bool:
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    return (u.scheme == "https" and u.hostname == "github.com"
            and not u.username and not u.password
            and len([p for p in u.path.split("/") if p]) >= 2)


def _scrub(text: str) -> str:
    tok = github_token()
    return text.replace(tok, "***") if tok and tok in text else text


def _auth_env(url: str | None) -> dict[str, str]:
    tok = github_token()
    if not tok or not url or urlsplit(url).hostname != "github.com":
        return {}
    b64 = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {b64}"}


async def get_remote(slug: str) -> str | None:
    db = await get_db()
    try:
        async with db.execute("SELECT github_remote FROM projects WHERE slug = ?",
                              (slug,)) as cur:
            row = await cur.fetchone()
        return row["github_remote"] if row else None
    finally:
        await db.close()


async def _ensure_origin(slug: str, url: str) -> None:
    """Self-heal: origin mirrors the stored URL (covers rows set before the
    remote API existed, or hand-edited in the DB)."""
    rc, out, _ = await run_git(slug, "remote", "get-url", "origin")
    if rc != 0:
        await run_git(slug, "remote", "add", "origin", url, check=True)
    elif out.strip() != url:
        await run_git(slug, "remote", "set-url", "origin", url, check=True)


async def set_remote(slug: str, url: str | None) -> None:
    """Persist the remote (DB) and mirror it onto the repo's origin so plain
    fetch/merge refs (origin/<branch>) exist. None disconnects."""
    await ensure_repo(slug)
    if url:
        await _ensure_origin(slug, url)
    else:
        rc, _, _ = await run_git(slug, "remote", "get-url", "origin")
        if rc == 0:
            await run_git(slug, "remote", "remove", "origin")
    db = await get_db()
    try:
        await db.execute("UPDATE projects SET github_remote = ? WHERE slug = ?",
                         (url, slug))
        await db.commit()
    finally:
        await db.close()


async def verify_remote(slug: str, url: str) -> None:
    """ls-remote reachability/auth check; raises RuntimeError (scrubbed) if not."""
    await ensure_repo(slug)
    rc, out, err = await run_git(slug, "ls-remote", "--heads", "--", url,
                                 extra_env=_auth_env(url), timeout=NET_TIMEOUT)
    if rc != 0:
        raise RuntimeError(_scrub((err or out).strip()))


async def current_branch(slug: str) -> str:
    rc, out, _ = await run_git(slug, "symbolic-ref", "--short", "-q", "HEAD")
    return out.strip() if rc == 0 and out.strip() else "main"


async def push_to_remote(slug: str) -> str:
    url = await get_remote(slug)
    if not url:
        raise ValueError("no remote connected for this project")
    await _ensure_origin(slug, url)
    branch = await current_branch(slug)
    rc, out, err = await run_git(slug, "push", "origin", f"HEAD:refs/heads/{branch}",
                                 extra_env=_auth_env(url), timeout=NET_TIMEOUT)
    if rc != 0:
        raise RuntimeError(_scrub((err or out).strip()))
    return _scrub((err or out).strip() or "pushed")   # git narrates on stderr


async def pull_from_remote(slug: str) -> str:
    """fetch + ff-only merge. Refuses on a dirty tree — never merge over the
    agent's uncommitted work."""
    url = await get_remote(slug)
    if not url:
        raise ValueError("no remote connected for this project")
    _, porcelain, _ = await run_git(slug, "status", "--porcelain")
    if porcelain.strip():
        raise ValueError("working tree has uncommitted changes — commit "
                         "(approve a request) or discard them before pulling")
    await _ensure_origin(slug, url)
    branch = await current_branch(slug)
    await run_git(slug, "fetch", "origin", extra_env=_auth_env(url),
                  timeout=NET_TIMEOUT, check=True)
    rc, out, err = await run_git(slug, "merge", "--ff-only",
                                 f"origin/{branch}")
    if rc != 0:
        raise RuntimeError(_scrub((err or out).strip()))
    return _scrub((out or err).strip() or "up to date")


async def ahead_behind(slug: str, fetch: bool = False) -> dict | None:
    """{'ahead': n, 'behind': m} vs origin/<branch>, or None if no remote ref
    is known yet. fetch=True refreshes origin first (network)."""
    url = await get_remote(slug)
    if not url:
        return None
    if fetch:
        await _ensure_origin(slug, url)
        await run_git(slug, "fetch", "origin", extra_env=_auth_env(url),
                      timeout=NET_TIMEOUT, check=True)
    branch = await current_branch(slug)
    rc, out, _ = await run_git(slug, "rev-list", "--left-right", "--count",
                               f"HEAD...origin/{branch}")
    if rc != 0:
        return None
    ahead, behind = (int(x) for x in out.split())
    return {"ahead": ahead, "behind": behind}


async def clone_repo(url: str, dest: Path) -> None:
    """git clone into a fresh dir; auth via env, so the token never lands in
    the clone's .git/config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update(_auth_env(url))
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--", url, str(dest),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                timeout=CLONE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"git clone timed out after {CLONE_TIMEOUT}s")
    if proc.returncode != 0:
        raise RuntimeError(_scrub(
            (stderr.decode(errors="replace") or stdout.decode(errors="replace")).strip()))


async def status_text(slug: str) -> str:
    rc, out, _ = await run_git(slug, "symbolic-ref", "--short", "-q", "HEAD")
    branch = out.strip() if rc == 0 else "detached"
    rc, last, _ = await run_git(slug, "log", "-1", "--oneline")
    last = last.strip() if rc == 0 else "no commits yet"
    _, porcelain, _ = await run_git(slug, "status", "--porcelain", "-uall")
    changes = porcelain.strip() or "clean"
    return f"branch: {branch}\nlast commit: {last}\nchanges:\n{changes}"


async def diff_text(slug: str, path: str | None = None, max_chars: int = 20000) -> str:
    rc, _, _ = await run_git(slug, "rev-parse", "--verify", "-q", "HEAD")
    args = ["diff", "--no-color"] + (["HEAD"] if rc == 0 else [])
    if path:
        args += ["--", path]
    _, diff, _ = await run_git(slug, *args)
    _, porcelain, _ = await run_git(slug, "status", "--porcelain", "-uall")
    untracked = [l[3:] for l in porcelain.splitlines() if l.startswith("??")]
    if path:
        untracked = [u for u in untracked
                     if u == path or u.startswith(path.rstrip("/") + "/")]
    parts = []
    if diff.strip():
        parts.append(diff.rstrip())
    if untracked:
        parts.append("untracked files (no diff until committed):\n"
                     + "\n".join(untracked))
    text = "\n".join(parts) or "no changes"
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [truncated]"
    return text


async def _fetch_request(db, rid: int) -> dict:
    async with db.execute("SELECT * FROM git_requests WHERE id = ?", (rid,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise KeyError(f"no git request #{rid}")
    return dict(row)


async def create_request(slug: str, message: str, paths: list[str] | None = None) -> dict:
    if not message or not message.strip():
        raise ValueError("commit message must not be empty")
    await ensure_repo(slug)
    _, porcelain, _ = await run_git(slug, "status", "--porcelain")
    if not porcelain.strip():
        raise ValueError("nothing to commit — the working tree is clean")
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO git_requests (project_slug, message, paths) VALUES (?, ?, ?)",
            (slug, message.strip(), json.dumps(paths) if paths else None))
        await db.commit()
        return await _fetch_request(db, cur.lastrowid)
    finally:
        await db.close()


async def create_remote_request(slug: str, url: str) -> dict:
    """The agent's path to a remote: file a request; the operator's approval
    verifies, connects, and pushes. The agent itself never touches the remote."""
    url = (url or "").strip()
    if not valid_remote(url):
        raise ValueError("remote must look like https://github.com/<owner>/<repo>")
    db = await get_db()
    try:
        async with db.execute(
                "SELECT id FROM git_requests WHERE project_slug = ? AND "
                "kind = 'remote' AND status = 'pending'", (slug,)) as cur:
            dup = await cur.fetchone()
        if dup:
            raise ValueError(f"remote request #{dup['id']} is already pending "
                             "for this project — wait for the operator")
        cur = await db.execute(
            "INSERT INTO git_requests (project_slug, kind, message) "
            "VALUES (?, 'remote', ?)", (slug, url))
        await db.commit()
        return await _fetch_request(db, cur.lastrowid)
    finally:
        await db.close()


async def _approve_remote(db, rid: int, row: dict) -> dict:
    """Operator approved a remote-connect request: verify auth/reach, connect,
    and push any existing commits. A push failure records but the connect stands."""
    slug, url = row["project_slug"], row["message"]
    try:
        await verify_remote(slug, url)
    except RuntimeError as e:
        await db.execute("UPDATE git_requests SET error = ? WHERE id = ?",
                         (f"verify failed: {e}", rid))
        await db.commit()
        raise                       # stays pending — retryable after fixing token/URL
    await set_remote(slug, url)
    error = None
    rc, _, _ = await run_git(slug, "rev-parse", "--verify", "-q", "HEAD")
    if rc == 0:                     # commits exist -> put them up now
        try:
            await push_to_remote(slug)
        except (RuntimeError, ValueError) as e:
            error = f"connected, but push failed: {e}"
    await db.execute(
        "UPDATE git_requests SET status = 'approved', error = ?, "
        "decided_at = datetime('now') WHERE id = ?", (error, rid))
    await db.commit()
    return await _fetch_request(db, rid)


async def approve_request(rid: int) -> dict:
    db = await get_db()
    try:
        row = await _fetch_request(db, rid)
        if row["status"] != "pending":
            raise ValueError(f"request #{rid} is {row['status']}, not pending")
        if row.get("kind") == "remote":
            return await _approve_remote(db, rid, row)
        slug, message = row["project_slug"], row["message"]
        paths = json.loads(row["paths"]) if row["paths"] else None
        await ensure_repo(slug)
        if paths:
            await run_git(slug, "add", "--", *paths, check=True)
        else:
            await run_git(slug, "add", "-A", check=True)
        try:
            await run_git(slug, "-c", "user.name=Jarvis",
                          "-c", "user.email=jarvis@atomos.local",
                          "commit", "-m", message, check=True)
        except RuntimeError as e:
            # leave the request pending (retryable) but record what went wrong
            await db.execute("UPDATE git_requests SET error = ? WHERE id = ?",
                             (str(e), rid))
            await db.commit()
            raise
        _, sha, _ = await run_git(slug, "rev-parse", "HEAD", check=True)
        error = None
        async with db.execute("SELECT github_remote FROM projects WHERE slug = ?",
                              (slug,)) as cur:
            prow = await cur.fetchone()
        if prow and prow["github_remote"]:
            try:
                await push_to_remote(slug)
            except (RuntimeError, ValueError) as e:
                error = f"push failed: {e}"  # commit stands
        await db.execute(
            "UPDATE git_requests SET status = 'approved', commit_sha = ?, error = ?, "
            "decided_at = datetime('now') WHERE id = ?", (sha.strip(), error, rid))
        await db.commit()
        return await _fetch_request(db, rid)
    finally:
        await db.close()


async def reject_request(rid: int) -> dict:
    db = await get_db()
    try:
        row = await _fetch_request(db, rid)
        if row["status"] != "pending":
            raise ValueError(f"request #{rid} is {row['status']}, not pending")
        await db.execute(
            "UPDATE git_requests SET status = 'rejected', decided_at = datetime('now') "
            "WHERE id = ?", (rid,))
        await db.commit()
        return await _fetch_request(db, rid)
    finally:
        await db.close()


async def list_requests(slug: str) -> list[dict]:
    db = await get_db()
    try:
        async with db.execute(
                "SELECT * FROM git_requests WHERE project_slug = ? ORDER BY id DESC",
                (slug,)) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
