"""Git gate: the agent can only *request* a commit; the host commits (and
pushes, when a remote is configured) after operator approval via the API.

The repo is the project dir itself. Staging metadata and runtime files are
kept out via a host-written .gitignore, and the agent can never write into
.git/ (staging + workspace endpoints refuse the path).
"""
import asyncio
import json
import os
from pathlib import Path

from . import sandbox, threatintel
from .config import settings
from .db import get_db

GIT_TIMEOUT = 30

GITIGNORE = ".staging/\n.workspace.json\n.context.json\ndata/\n"


def _project_dir(slug: str) -> Path:
    return settings.projects_dir / slug


async def run_git(slug: str, *args: str, check: bool = False) -> tuple[int, str, str]:
    """git -C <project dir> <args>. Never a shell; env stripped of GIT_* surprises."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(_project_dir(slug)), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT}s")
    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(err or out).strip()}")
    return proc.returncode, out, err


async def ensure_repo(slug: str) -> None:
    d = _project_dir(slug)
    if not (d / ".git").exists():
        await run_git(slug, "init", "-q", check=True)
        await run_git(slug, "config", "user.name", "Jarvis", check=True)
        await run_git(slug, "config", "user.email", "jarvis@atomos.local", check=True)
    gitignore = d / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE)


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


async def latest_run_verdict(slug: str) -> dict | None:
    """Deterministic verdict of the project's most recent gated sandbox run
    (None if it never ran). Same classifier the console uses — never an LLM."""
    db = await get_db()
    try:
        async with db.execute(
            "SELECT r.id FROM runs r JOIN projects p ON p.id = r.project_id "
            "WHERE p.slug = ? ORDER BY r.id DESC LIMIT 1", (slug,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    if not row:
        return None
    rid = row["id"] if not isinstance(row, tuple) else row[0]
    p = settings.vm_dir / "captures" / f"gate-{rid}-evidence.json"
    if not p.is_file():
        return None
    try:
        ev = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    c = sandbox.classify(ev, await sandbox.rules_index(), threatintel.load())
    return {"run_id": rid, "verdict": c["verdict"], "rule": c["rule"],
            "headline": c["headline"]}


async def approve_request(rid: int, force: bool = False) -> dict:
    db = await get_db()
    try:
        row = await _fetch_request(db, rid)
        if row["status"] != "pending":
            raise ValueError(f"request #{rid} is {row['status']}, not pending")
        slug, message = row["project_slug"], row["message"]
        # Anti-malware gate (M4): refuse to commit/push if the project's last
        # sandbox run detonated as critical, unless the operator forces it. The
        # verdict is deterministic (classifier over host captures), so this is a
        # hard safety check, not an advisory one. Nothing is committed on block.
        if not force:
            v = await latest_run_verdict(slug)
            if v and v["verdict"] == "crit":
                raise PermissionError(
                    f"blocked — last sandbox run #{v['run_id']} was CRITICAL "
                    f"({v['rule']}): {v['headline']}. Re-run it clean in the sandbox, "
                    f"or approve with force to override.")
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
        remote = prow["github_remote"] if prow else None
        if remote:
            rc, out, err = await run_git(slug, "push", remote, "HEAD")
            if rc != 0:
                error = f"push failed: {(err or out).strip()}"  # commit stands
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
