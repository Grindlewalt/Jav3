"""Git gate: the agent can only *request* a commit; the host commits (and
pushes, when a remote is configured) after operator approval via the API.

The repo is the project dir itself. Staging metadata and runtime files are
kept out via a host-written .gitignore, and the agent can never write into
.git/ (writes + workspace endpoints refuse the path).
"""
import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings
from .db import get_db

log = logging.getLogger("jav3.gitgate")

GIT_TIMEOUT = 30
NET_TIMEOUT = 120       # push / fetch / ls-remote cross the internet on a Pi
CLONE_TIMEOUT = 300

# The harness's own files at the project root: never part of a commit or a push,
# whatever the project's .gitignore says (the agent can replace the host-written
# one). `.todo.md` is the todo_update list (`.todo-archive.md` its pruned items),
# `.plan.json` the plan checklist, `runs/<job>/*.md` the rollups an orchestrator
# or research run writes. Used for .gitignore, for .git/info/exclude (which
# `git add -A` honours and the agent cannot write: the add in approve_request and
# gitea's snapshot rely on that one), and for unstage_harness below.
RUNTIME_FILES = (".staging", ".workspace.json", ".context.json", ".todo.md",
                 ".todo-archive.md", ".plan.json", "data")
ROLLUP_GLOB = "runs/*/*.md"     # narrow on purpose: a project's own runs/ data stays committable
_EXCLUDE_PATTERNS = tuple(f"/{n}" for n in RUNTIME_FILES) + (f"/{ROLLUP_GLOB}",)
# what a commit must never stage even when tracked, as pathspecs (data/ is kept
# out of new work above but is not policed once tracked: a project may hold real
# data there on purpose)
HARNESS_PATHSPECS = (".staging", ".workspace.json", ".context.json", ".todo.md",
                     ".todo-archive.md", ".plan.json", f":(glob){ROLLUP_GLOB}")
# The managed block of a project's .gitignore. Everything between the markers is
# rewritten by ensure_gitignore, so a project's own rules go outside it.
MANAGED_BEGIN = ("# >>> Jav3 harness files: managed by Jav3 (rewritten on load); "
                 "your own rules go outside this block")
MANAGED_END = "# <<< Jav3 harness files"
GITIGNORE = "\n".join([
    MANAGED_BEGIN, ".staging/", ".workspace.json", ".context.json", ".todo.md",
    ".todo-archive.md", "/.plan.json", f"/{ROLLUP_GLOB}", "data/", MANAGED_END]) + "\n"
_EXCLUDE = "# Jav3 runtime files (written by the host)\n" + "".join(
    f"{p}\n" for p in _EXCLUDE_PATTERNS)


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


async def flush_guest_writes(slug: str) -> None:
    """Bring this turn's file writes to the host before a git tool reads them.

    A turn that runs in a guest VM buffers write_file/edit_file there (.staging)
    and hands the buffer back only after its final message. Until then the host
    repo cannot see the files: git_status said "clean", git_commit_request said
    "nothing to commit", and git_push_request snapshotted a tree WITHOUT the
    agent's new files (a PR holding one host-side journal line, 2026-09-29).
    The guest's `pull` returns the buffer without clearing it and applying it
    again is idempotent, so this is safe mid-turn and again at turn end. A no-op
    outside a brokered guest tool call (HTTP, host-run turns, tests)."""
    from .agent import budget as budget_mod
    from .vm import broker
    op = budget_mod.active_op_id.get()
    if not op or broker.get_turn(op) is None:
        return
    from .vm import guest_turn
    try:
        await asyncio.wait_for(guest_turn.pull_writes(slug), timeout=60)
    except Exception as e:  # noqa: BLE001 — the git tool still answers, from what the host has
        log.warning("could not pull the guest's writes for %s: %s", slug, e)


def ensure_gitignore(slug: str) -> bool:
    """Make sure the project's .gitignore carries the current managed block of
    harness files; True if the file was written. Idempotent: no block yet means
    it is added at the end (nothing of the project's own rules is touched), an
    old block is replaced in place, and a current one is left alone, so calling
    it on every load costs one small read. Never raises: a project whose
    directory or .gitignore cannot be used just goes without."""
    path = _project_dir(slug) / ".gitignore"
    try:
        if not path.parent.is_dir():
            return False
        have = path.read_text() if path.exists() else ""
        lines = have.splitlines()
        begin = next((i for i, l in enumerate(lines)
                      if l.startswith("# >>> Jav3 harness files")), None)
        end = next((i for i, l in enumerate(lines)
                    if begin is not None and i > begin
                    and l.startswith("# <<< Jav3 harness files")), None)
        block = GITIGNORE.splitlines()
        if begin is not None and end is not None:
            new = lines[:begin] + block + lines[end + 1:]
        else:
            # none, or a damaged one (an end marker lost): add a whole block at
            # the end; a half block left behind only repeats lines harmlessly
            new = lines + ([""] if lines and lines[-1].strip() else []) + block
        text = "\n".join(new) + "\n"
        if text == have:
            return False
        path.write_text(text)
        return True
    except (OSError, UnicodeDecodeError) as e:
        log.warning("could not update %s: %s", path, e)
        return False


async def tracked_harness(slug: str, extra_env: dict[str, str] | None = None) -> list[str]:
    """Harness files git already tracks (an old commit took them in): they
    are no longer ignored, so every `git add -A` stages their changes."""
    rc, out, _ = await run_git(slug, "ls-files", "-z", "--", *HARNESS_PATHSPECS,
                               extra_env=extra_env)
    return [p for p in out.split("\0") if p] if rc == 0 else []


async def unstage_harness(slug: str, extra_env: dict[str, str] | None = None) -> list[str]:
    """After a `git add`: put the harness files back to what HEAD has, so a file
    that is tracked anyway (the history is the operator's to rewrite, not ours)
    does not ride into the commit. Returns the files it put back."""
    tracked = await tracked_harness(slug, extra_env)
    if tracked:
        await run_git(slug, "reset", "-q", "--", *tracked, extra_env=extra_env)
    return tracked


async def harness_notice(slug: str) -> list[str]:
    """What to tell the agent about harness files that are tracked: one line per
    file, and each file only ONCE per project (remembered in .git, which no
    agent tool can write)."""
    tracked = await tracked_harness(slug)
    if not tracked:
        return []
    told_path = _project_dir(slug) / ".git" / "jav3-harness-told"
    try:
        told = set(told_path.read_text().split("\n")) if told_path.exists() else set()
        fresh = [t for t in tracked if t not in told]
        if fresh:
            told_path.write_text("\n".join(sorted(told | set(fresh))) + "\n")
    except OSError:
        fresh = tracked
    return [f"{t} is tracked; run `git rm --cached {t}` to stop committing it "
            "(Jav3 leaves it out of every commit meanwhile)" for t in fresh]


# per-slug init lock: git_status/git_diff are read_only-flagged, so the loop
# may run them concurrently — two first-touches must not race `git init`
_repo_locks: dict[str, asyncio.Lock] = {}


async def ensure_repo(slug: str) -> None:
    lock = _repo_locks.setdefault(slug, asyncio.Lock())
    async with lock:
        d = _project_dir(slug)
        if not (d / ".git").exists():
            await run_git(slug, "init", "-q", check=True)
            await run_git(slug, "config", "user.name", "Jav3", check=True)
            await run_git(slug, "config", "user.email", settings.git_author_email,
                          check=True)
        ensure_gitignore(slug)
        # an `rm`/rewrite of .gitignore must not let the runtime files into a
        # commit: info/exclude lives inside .git, which no agent tool can write
        exclude = d / ".git" / "info" / "exclude"
        try:
            if not exclude.exists() or exclude.read_text() != _EXCLUDE:
                exclude.parent.mkdir(exist_ok=True)
                exclude.write_text(_EXCLUDE)
        except OSError as e:
            log.warning("could not write %s: %s", exclude, e)


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


def _requesting_turn() -> int | None:
    """The conversation whose turn filed a request (the broker restores it for
    a guest tool call); None over HTTP. Recorded so the agents tree can show
    which node is waiting on this approval."""
    from . import runtime
    return runtime.conversation_id.get()


async def create_request(slug: str, message: str, paths: list[str] | None = None) -> dict:
    if not message or not message.strip():
        raise ValueError("commit message must not be empty")
    await ensure_repo(slug)
    await flush_guest_writes(slug)      # this turn's write_file/edit_file, still in the VM
    _, porcelain, _ = await run_git(slug, "status", "--porcelain")
    tracked = await tracked_harness(slug)
    if tracked:
        # a tracked harness file shows as modified on every turn but is never
        # staged (unstage_harness): it is not something to commit
        porcelain = "\n".join(l for l in porcelain.splitlines() if l[3:].strip('"') not in tracked)
    if not porcelain.strip():
        raise ValueError("nothing to commit — the working tree is clean: every file in "
                         "the project already matches the last commit. Write or edit "
                         "a file first.")
    db = await get_db()
    try:
        # BUILD-08: a project has one pending commit request. Older ones named a
        # tree that no longer exists (one claimed a rename the commit could not
        # record, one with no paths would `git add -A` whatever is there at
        # approval), so the new request replaces them, and says so.
        async with db.execute(
                "SELECT id, message FROM git_requests WHERE project_slug = ? "
                "AND kind = 'commit' AND status = 'pending' ORDER BY id", (slug,)) as cur:
            old = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            "INSERT INTO git_requests (project_slug, message, paths, conversation_id) "
            "VALUES (?, ?, ?, ?)",
            (slug, message.strip(), json.dumps(paths) if paths else None,
             _requesting_turn()))
        for o in old:
            await db.execute(
                "UPDATE git_requests SET status = 'rejected', error = ?, "
                "decided_at = datetime('now') WHERE id = ? AND status = 'pending'",
                (f"replaced by request #{cur.lastrowid}", o["id"]))
        await db.commit()
        row = await _fetch_request(db, cur.lastrowid)
        row["replaced"] = old
        row["harness_notice"] = await harness_notice(slug)
        return row
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
            "INSERT INTO git_requests (project_slug, kind, message, conversation_id) "
            "VALUES (?, 'remote', ?, ?)", (slug, url, _requesting_turn()))
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


async def _own_request(db, rid: int, slug: str | None) -> dict:
    """The request, checked to belong to the project in the URL."""
    row = await _fetch_request(db, rid)
    if slug is not None and row["project_slug"] != slug:
        raise KeyError(f"no git request #{rid} in project {slug}")
    return row


async def approve_request(rid: int, slug: str | None = None) -> dict:
    db = await get_db()
    try:
        row = await _own_request(db, rid, slug)
        if row["status"] != "pending":
            raise ValueError(f"request #{rid} is {row['status']}, not pending")
        if row.get("kind") == "remote":
            return await _approve_remote(db, rid, row)
        if row.get("kind") == "push":
            from . import gitea
            return await gitea.approve_push(db, rid, row)
        slug, message = row["project_slug"], row["message"]
        paths = json.loads(row["paths"]) if row["paths"] else None
        await ensure_repo(slug)
        from . import gitea
        if gitea.enabled():
            # take in anything merged in Gitea first, so the push after this
            # commit is a fast-forward (only HEAD + index move; files stay)
            try:
                await gitea.reconcile(slug)
                await gitea.sync_main(slug)
            except (RuntimeError, ValueError):
                pass
        if paths:
            await run_git(slug, "add", "--", *paths, check=True)
        else:
            await run_git(slug, "add", "-A", check=True)     # RUNTIME_FILES: info/exclude
        await unstage_harness(slug)         # ...which does not cover a harness file already tracked
        rc, _, _ = await run_git(slug, "diff", "--cached", "--quiet")
        rc_head, head, _ = await run_git(slug, "rev-parse", "--verify", "-q", "HEAD")
        if rc == 0 and rc_head == 0:
            # nothing left to commit: a pull request merged since (or an earlier
            # approval) already put these files into main. Close the request
            # instead of leaving it pending with a git error nobody can fix.
            await db.execute(
                "UPDATE git_requests SET status = 'approved', commit_sha = ?, error = ?, "
                "decided_at = datetime('now') WHERE id = ?",
                (head.strip(), "nothing new to commit: these changes are already in main", rid))
            await db.commit()
            return await _fetch_request(db, rid)
        try:
            await run_git(slug, "-c", "user.name=Jav3",
                          "-c", f"user.email={settings.git_author_email}",
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
        if gitea.enabled():
            # main mirrors to Gitea as the operator (protected against the bot)
            try:
                await gitea.ensure_remote_repo(slug)
                gerr = await gitea.push_main(slug)
            except (RuntimeError, ValueError) as e:
                gerr = f"committed; main not pushed to Gitea: {gitea.scrub(str(e))}"
            if gerr:
                error = f"{error}; {gerr}" if error else gerr
        await db.execute(
            "UPDATE git_requests SET status = 'approved', commit_sha = ?, error = ?, "
            "decided_at = datetime('now') WHERE id = ?", (sha.strip(), error, rid))
        await db.commit()
        return await _fetch_request(db, rid)
    finally:
        await db.close()


async def reject_request(rid: int, slug: str | None = None) -> dict:
    db = await get_db()
    try:
        row = await _own_request(db, rid, slug)
        if row["status"] != "pending":
            raise ValueError(f"request #{rid} is {row['status']}, not pending")
        if row.get("kind") == "push":
            from . import gitea
            return await gitea.reject_push(db, rid, row)
        await db.execute(
            "UPDATE git_requests SET status = 'rejected', decided_at = datetime('now') "
            "WHERE id = ?", (rid,))
        await db.commit()
        return await _fetch_request(db, rid)
    finally:
        await db.close()


async def list_requests(slug: str) -> list[dict]:
    from . import gitea
    await gitea.reconcile(slug)     # push rows follow merges/closes done in Gitea
    db = await get_db()
    try:
        async with db.execute(
                "SELECT * FROM git_requests WHERE project_slug = ? ORDER BY id DESC",
                (slug,)) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
