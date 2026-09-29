"""Gitea on the host: one private repo per project, and the agent's only way
to put work up for review.

Custody. Two API tokens live 0600 in Jav3's config dir: the operator's (admin)
and the `jav3-agent` bot's. Both are read here, host-side, per call. Neither is
ever written into a repo's .git/config, argv, the DB, a tool result or a box:
git gets auth as a per-invocation `http.extraheader` through GIT_CONFIG_* env
(the same trick as gitgate's GitHub push), and every surfaced string passes
scrub().

The agent's path (git_push_request): the HOST snapshots the project's live
files into a commit on top of main (a temporary index; main and the working
tree are untouched), pushes it AS THE BOT to a branch whose name the host
builds (`agent/<8 hex>`, never model-chosen), opens a pull request into main,
and files a git_requests row of kind 'push'. Approve = merge the PR as the
operator; reject = close it and delete the branch. Main is protected in Gitea:
only the operator may push or merge, the bot is not on either whitelist.
The box never holds a credential and its git state is discarded, so there is
no `git push` from inside it at all.
"""
import asyncio
import base64
import logging
import os
import re
import secrets as pysecrets
from pathlib import Path

import httpx

from . import gitgate
from .config import settings
from .db import get_db

log = logging.getLogger("jav3.gitea")

REMOTE = "gitea"                    # host repo remote name (origin stays GitHub's)
BRANCH_PREFIX = "agent/"
_AGENT_REF = re.compile(r"^agent/[0-9a-f]{8}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
API_TIMEOUT = 30
MERGE_CHECK_TRIES = 3               # a merge Gitea says "try again later" to is retried
MERGE_CHECK_WAIT = 2.0              # ...this many seconds apart (a conflict never clears)

# tests swap in httpx.MockTransport; None = the real network
_transport: httpx.AsyncBaseTransport | None = None


class GiteaError(RuntimeError):
    """Gitea (or git talking to it) said no or could not be reached. The text
    is scrubbed and written for the operator or the model to act on."""


class GiteaUnreachable(GiteaError):
    """Nothing answered: the service is down, or the port is wrong."""


class GiteaRefused(GiteaError):
    """Gitea answered and declined a merge (conflict, protection). The pull
    request is untouched and the Jav3 request stays pending."""


class GiteaOff(RuntimeError):
    pass


def _api_message(r: httpx.Response) -> str:
    """The `message` of a Gitea error body, not the whole JSON blob."""
    try:
        m = r.json().get("message")
    except (ValueError, AttributeError):
        m = None
    return str(m or r.text or "no detail")[:300].strip()


def _gist(out: str) -> str:
    """The lines of git's stderr that say why, without the ref and URL chatter."""
    keep = []
    for line in (out or "").splitlines():
        t = re.sub(r"^(remote:\s*)?(error:\s*)?", "", line.strip()).strip()
        if t and not t.startswith(("To http", "failed to push", "! [", "hint:")):
            keep.append(t)
    return "; ".join(keep)[:300]


def explain_git_failure(out: str, who: str = "the agent bot") -> GiteaError:
    """Raw `git push` stderr -> what happened. Unknown text passes through."""
    low = (out or "").lower()
    if any(k in low for k in ("failed to connect", "connection refused", "could not resolve",
                              "couldn't connect", "timed out")):
        return GiteaUnreachable(f"Gitea isn't answering at {api_base()}: {out}")
    if "protected branch" in low or "not allowed to push" in low or "pre-receive hook declined" in low:
        return GiteaError(f"Gitea refused the push because the branch is protected "
                          f"({_gist(out)}). Only the operator can push to main.")
    if any(k in low for k in ("authentication failed", "invalid username", "401", "403",
                              "could not read username")):
        return GiteaError(f"Gitea rejected {who}'s token, so nothing was pushed. The "
                          "operator can re-run `python -m backend.cli gitea-setup` to reissue it.")
    return GiteaError(out or "git push failed with no output")


# --- configuration -------------------------------------------------------------

def _read(p: Path) -> str | None:
    try:
        t = Path(p).read_text().strip()
    except OSError:
        return None
    return t or None


def admin_token() -> str | None:
    return _read(settings.gitea_admin_token_path)


def bot_token() -> str | None:
    return _read(settings.gitea_bot_token_path)


def owner() -> str:
    return (settings.gitea_owner or "").strip()


def bot_user() -> str:
    return (settings.gitea_bot_user or "jav3-agent").strip()


def enabled() -> bool:
    """On only when switched on AND set up (both tokens + an owner)."""
    return bool(settings.gitea_enabled and owner() and admin_token() and bot_token())


def api_base() -> str:
    """Host-side address: the API and git are always reached on loopback."""
    return f"http://127.0.0.1:{settings.gitea_port}"


def public_url() -> str:
    """The address for the operator's browser (links in the dashboard)."""
    if settings.gitea_url.strip():
        return settings.gitea_url.strip().rstrip("/")
    try:
        from .lan import lan_ips
        ips = lan_ips()
    except Exception:
        ips = []
    return f"http://{ips[0] if ips else '127.0.0.1'}:{settings.gitea_port}"


def scrub(text: str) -> str:
    for tok in (admin_token(), bot_token()):
        if tok and tok in (text or ""):
            text = text.replace(tok, "***")
    return text


def repo_url(slug: str) -> str:
    """Clean (credential-free) git URL of a project's repo, host-side."""
    return f"{api_base()}/{owner()}/{slug}.git"


def web_url(slug: str) -> str:
    return f"{public_url()}/{owner()}/{slug}"


def _auth_env(user: str, token: str | None) -> dict[str, str]:
    if not token:
        raise GiteaOff("Gitea token missing")
    b64 = base64.b64encode(f"{user}:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {b64}"}


def operator_env() -> dict[str, str]:
    return _auth_env(owner(), admin_token())


def bot_env() -> dict[str, str]:
    return _auth_env(bot_user(), bot_token())


def check_agent_ref(branch: str) -> str:
    """The bot may only ever push `agent/<8 hex>`. Anything else (main, a
    tag, a refspec) is refused before git runs."""
    if not isinstance(branch, str) or not _AGENT_REF.match(branch):
        raise ValueError(f"refused: the agent may only push agent/* branches, not {branch!r}")
    return branch


def new_branch() -> str:
    return check_agent_ref(BRANCH_PREFIX + pysecrets.token_hex(4))


# --- API -----------------------------------------------------------------------

async def api(method: str, path: str, *, token: str | None = None, json_body=None,
              allow: tuple[int, ...] = ()) -> httpx.Response:
    tok = token or admin_token()
    if not tok:
        raise GiteaOff("Gitea admin token missing")
    async with httpx.AsyncClient(base_url=api_base() + "/api/v1", transport=_transport,
                                 timeout=API_TIMEOUT) as c:
        try:
            r = await c.request(method, path, json=json_body,
                                headers={"Authorization": f"token {tok}",
                                         "Accept": "application/json"})
        except httpx.HTTPError as e:
            detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            raise GiteaUnreachable(scrub(
                f"Gitea isn't answering at {api_base()} ({detail})")) from None
    if r.status_code >= 400 and r.status_code not in allow:
        raise GiteaError(scrub(f"Gitea refused {method} {path} ({r.status_code}): "
                               f"{_api_message(r)}"))
    return r


async def version() -> str | None:
    try:
        r = await api("GET", "/version")
        return r.json().get("version")
    except (GiteaError, GiteaOff, ValueError):
        return None


# --- repos ---------------------------------------------------------------------

def _protection(o: str) -> dict:
    return {"rule_name": "main", "branch_name": "main",
            "enable_push": True, "enable_push_whitelist": True,
            "push_whitelist_usernames": [o],
            "enable_merge_whitelist": True, "merge_whitelist_usernames": [o],
            "enable_status_check": False, "block_on_rejected_reviews": False}


async def ensure_remote_repo(slug: str) -> dict:
    """Idempotent: the private repo `<owner>/<slug>`, the bot as a write
    collaborator (branches only: main is protected against it), and main's
    protection rule re-asserted every time."""
    if not _SLUG.match(slug or ""):
        raise ValueError(f"bad project slug {slug!r}")
    o = owner()
    r = await api("GET", f"/repos/{o}/{slug}", allow=(404,))
    if r.status_code == 404:
        r = await api("POST", "/user/repos", json_body={
            "name": slug, "private": True, "auto_init": False,
            "default_branch": "main",
            "description": f"Jav3 project {slug}"})
    repo = r.json()
    await api("PUT", f"/repos/{o}/{slug}/collaborators/{bot_user()}",
              json_body={"permission": "write"})
    rule = _protection(o)
    r = await api("GET", f"/repos/{o}/{slug}/branch_protections/main", allow=(404,))
    if r.status_code == 404:
        await api("POST", f"/repos/{o}/{slug}/branch_protections", json_body=rule)
    else:
        await api("PATCH", f"/repos/{o}/{slug}/branch_protections/main",
                  json_body={k: v for k, v in rule.items()
                             if k not in ("rule_name", "branch_name")})
    return repo


async def _ensure_git_remote(slug: str) -> None:
    """The host repo's `gitea` remote, credential-free."""
    url = repo_url(slug)
    rc, out, _ = await gitgate.run_git(slug, "remote", "get-url", REMOTE)
    if rc != 0:
        await gitgate.run_git(slug, "remote", "add", REMOTE, url, check=True)
    elif out.strip() != url:
        await gitgate.run_git(slug, "remote", "set-url", REMOTE, url, check=True)


async def ensure_repo(slug: str) -> dict:
    """Remote repo + local remote + main on Gitea (pushed as the operator)."""
    if not enabled():
        raise GiteaOff("Gitea is not set up")
    await gitgate.ensure_repo(slug)
    repo = await ensure_remote_repo(slug)
    await _ensure_git_remote(slug)
    serr = await sync_main(slug)
    perr = await push_main(slug)
    if perr:
        raise GiteaError(serr or perr)
    return repo


DIVERGED = ("the host's main and Gitea's main have diverged (each has commits the "
            "other lacks); the operator has to reconcile them by hand")


async def _git_net(slug: str, *args: str, env: dict) -> tuple[int, str]:
    rc, out, err = await gitgate.run_git(slug, *args, extra_env=env,
                                         timeout=gitgate.NET_TIMEOUT)
    return rc, scrub((err or out).strip())


async def _has_head(slug: str) -> bool:
    rc, _, _ = await gitgate.run_git(slug, "rev-parse", "--verify", "-q", "HEAD")
    return rc == 0


async def _remote_main(slug: str) -> str | None:
    rc, out, err = await gitgate.run_git(slug, "ls-remote", "--heads", "--", repo_url(slug),
                                         "refs/heads/main", extra_env=operator_env(),
                                         timeout=gitgate.NET_TIMEOUT)
    if rc != 0:
        raise explain_git_failure(scrub((err or out).strip()), "the operator")
    line = out.split()
    return line[0] if line else None


async def sync_main(slug: str) -> str | None:
    """Bring the host's main up to Gitea's main after a merge there. Only a
    fast-forward, and only HEAD + index move (`reset --mixed`): the live files
    are never touched, so work the agent did since stays as changes. Returns
    an error string when the two have diverged (nothing is changed then)."""
    if not await _has_head(slug):
        return None
    remote = await _remote_main(slug)
    if not remote:
        return None
    _, head, _ = await gitgate.run_git(slug, "rev-parse", "HEAD")
    if remote == head.strip():
        return None
    rc, out = await _git_net(slug, "fetch", "-q", "--", repo_url(slug),
                             "+refs/heads/main:refs/remotes/gitea/main", env=operator_env())
    if rc != 0:
        return f"fetch from Gitea failed: {out}"
    rc, _, _ = await gitgate.run_git(slug, "merge-base", "--is-ancestor", "HEAD", remote)
    if rc != 0:
        rc2, _, _ = await gitgate.run_git(slug, "merge-base", "--is-ancestor", remote, "HEAD")
        if rc2 == 0:
            return None             # the host is ahead; push_main carries it up
        return DIVERGED
    await gitgate.run_git(slug, "reset", "-q", "--mixed", remote, check=True)
    return None


async def _sync_quiet(slug: str) -> str | None:
    """sync_main for after a merge: a failure is recorded, never raised (the
    merge already happened)."""
    try:
        err = await sync_main(slug)
    except (GiteaError, GiteaOff, RuntimeError) as e:
        err = str(e)
    return f"merged; host main not updated: {err}" if err else None


async def push_main(slug: str) -> str | None:
    """Push the host's main to Gitea as the operator. Error string or None."""
    if not await _has_head(slug):
        return None
    await _ensure_git_remote(slug)
    rc, out = await _git_net(slug, "push", "-q", REMOTE, "HEAD:refs/heads/main",
                             env=operator_env())
    if rc == 0:
        return None
    return f"push of main to Gitea failed: {explain_git_failure(out, 'the operator')}"


# --- the agent's push request --------------------------------------------------

_NEVER_SNAPSHOT = (":(exclude).staging", ":(exclude).workspace.json", ":(exclude).context.json")


async def _snapshot_commit(slug: str, message: str) -> str:
    """A commit of the live files on top of HEAD, built in a throwaway index:
    main, the real index and the working tree are untouched."""
    d = settings.projects_dir / slug
    idx = d / ".git" / f"jav3-push-{pysecrets.token_hex(4)}.index"
    env = {"GIT_INDEX_FILE": str(idx),
           "GIT_AUTHOR_NAME": bot_user(), "GIT_AUTHOR_EMAIL": f"{bot_user()}@localhost",
           "GIT_COMMITTER_NAME": "Jav3", "GIT_COMMITTER_EMAIL": settings.git_author_email}
    try:
        await gitgate.run_git(slug, "read-tree", "HEAD", extra_env=env, check=True)
        # runtime files stay out even if the agent overwrote the host's .gitignore
        await gitgate.run_git(slug, "add", "-A", "--", ".", *_NEVER_SNAPSHOT,
                              extra_env=env, check=True)
        _, tree, _ = await gitgate.run_git(slug, "write-tree", extra_env=env, check=True)
        _, base, _ = await gitgate.run_git(slug, "rev-parse", "HEAD^{tree}", check=True)
        if tree.strip() == base.strip():
            raise ValueError(
                "nothing to push: every file in the project already matches main on "
                "Gitea. Write or edit a file first, then file the request again.")
        _, sha, _ = await gitgate.run_git(slug, "commit-tree", tree.strip(), "-p", "HEAD",
                                          "-m", message, extra_env=env, check=True)
        return sha.strip()
    finally:
        idx.unlink(missing_ok=True)


async def push_agent_branch(slug: str, sha: str, branch: str) -> None:
    """Push exactly one commit to exactly one agent/* branch, as the bot."""
    check_agent_ref(branch)
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise ValueError("bad commit id")
    rc, out = await _git_net(slug, "push", "-q", "--", repo_url(slug),
                             f"{sha}:refs/heads/{branch}", env=bot_env())
    if rc != 0:
        raise explain_git_failure(out)


async def _delete_branch(slug: str, branch: str) -> None:
    check_agent_ref(branch)
    await api("DELETE", f"/repos/{owner()}/{slug}/branches/{branch}", allow=(404,))


async def create_push_request(slug: str, title: str, description: str = "") -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("title must not be empty")
    title = title.splitlines()[0][:200]
    description = (description or "").strip()[:8000]
    if not enabled():
        raise GiteaOff("Gitea isn't set up on this Jav3")
    await gitgate.ensure_repo(slug)
    await gitgate.flush_guest_writes(slug)      # this turn's writes are still in the VM
    if not await _has_head(slug):
        raise ValueError("the project has no commits yet — use git_commit_request first")
    await ensure_repo(slug)
    await reconcile(slug)       # settle requests merged/closed in Gitea, and take main in
    branch = new_branch()
    message = title + ("\n\n" + description if description else "")
    sha = await _snapshot_commit(slug, message)
    tree = await _tree_of(slug, sha)
    others = await _pending_pushes(slug)
    for o in others:
        try:
            same = bool(o["commit_sha"]) and await _tree_of(slug, o["commit_sha"]) == tree
        except RuntimeError:        # an old commit git no longer has: cannot be the same
            same = False
        if same:
            raise ValueError(
                f"push request #{o['id']} (pull request #{o['pr_number']}) already holds "
                "exactly these changes and is waiting for the operator. Don't file it "
                "again; carry on, and tell the operator it is waiting.")
    _, stat, _ = await gitgate.run_git(slug, "diff", "--stat", "--no-color", "HEAD", sha)
    stat = stat.strip()[-4000:]
    _, names, _ = await gitgate.run_git(slug, "diff", "--name-status", "--no-color", "HEAD", sha)
    await push_agent_branch(slug, sha, branch)
    try:
        body = (description + "\n\n" if description else "") + \
            "Filed by the Jav3 agent with git_push_request.\n\n```\n" + stat + "\n```"
        r = await api("POST", f"/repos/{owner()}/{slug}/pulls", token=bot_token(),
                      json_body={"head": branch, "base": "main",
                                 "title": title, "body": body})
        pr = r.json()
    except (GiteaError, ValueError):
        await _drop_branch_quietly(slug, branch)
        raise
    number = pr.get("number")
    pr_url = f"{web_url(slug)}/pulls/{number}"
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO git_requests (project_slug, kind, message, commit_sha, "
            "conversation_id, branch, pr_number, pr_url, summary) "
            "VALUES (?, 'push', ?, ?, ?, ?, ?, ?, ?)",
            (slug, message, sha, gitgate._requesting_turn(), branch, number,
             pr_url, stat))
        await db.commit()
        row = await gitgate._fetch_request(db, cur.lastrowid)
    except Exception:
        # a PR with no request row would sit in Gitea unseen by the Review Center
        await _close_pr_quietly(slug, number)
        await _drop_branch_quietly(slug, branch)
        raise
    finally:
        await db.close()
    # not stored: for the tool's reply
    row["files"] = [l.replace("\t", " ") for l in names.strip().splitlines()][:20]
    row["others"] = [{"id": o["id"], "pr_number": o["pr_number"]} for o in others]
    return row


async def _tree_of(slug: str, commit: str) -> str:
    _, out, _ = await gitgate.run_git(slug, "rev-parse", f"{commit}^{{tree}}", check=True)
    return out.strip()


async def _pending_pushes(slug: str) -> list[dict]:
    db = await get_db()
    try:
        async with db.execute(
                "SELECT id, pr_number, commit_sha FROM git_requests WHERE project_slug = ? "
                "AND kind = 'push' AND status = 'pending' ORDER BY id", (slug,)) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _drop_branch_quietly(slug: str, branch: str) -> None:
    try:
        await _delete_branch(slug, branch)
    except (GiteaError, GiteaOff):
        log.warning("could not delete %s in %s after a failed push request", branch, slug)


async def _close_pr_quietly(slug: str, number) -> None:
    try:
        await api("PATCH", f"/repos/{owner()}/{slug}/pulls/{number}",
                  json_body={"state": "closed"})
    except (GiteaError, GiteaOff):
        log.warning("could not close pull request #%s in %s", number, slug)


async def _pull(slug: str, number: int) -> dict | None:
    """The pull request, or None when Gitea no longer has it (deleted there)."""
    r = await api("GET", f"/repos/{owner()}/{slug}/pulls/{number}", allow=(404,))
    return None if r.status_code == 404 else r.json()


async def _finish(db, rid: int, status: str, sha: str | None, error: str | None) -> dict:
    await db.execute(
        "UPDATE git_requests SET status = ?, commit_sha = COALESCE(?, commit_sha), "
        "error = ?, decided_at = datetime('now') WHERE id = ?", (status, sha, error, rid))
    await db.commit()
    return await gitgate._fetch_request(db, rid)


async def approve_push(db, rid: int, row: dict) -> dict:
    """Operator approved: merge the PR as the operator, then fast-forward the
    host's main to it."""
    if not enabled():
        raise ValueError("Gitea is not set up — approve or close the PR in Gitea")
    slug, number = row["project_slug"], row["pr_number"]
    pr = await _pull(slug, number)
    if pr is None:
        await _finish(db, rid, "rejected", None, "the pull request no longer exists in Gitea")
        raise ValueError(f"pull request #{number} no longer exists in Gitea (deleted there), "
                         "so this request was closed. Nothing was merged.")
    if not pr.get("merged"):
        if pr.get("state") == "closed":
            await _finish(db, rid, "rejected", None, "the pull request was closed in Gitea")
            raise ValueError("the pull request was already closed in Gitea, so this "
                             "request was closed. Nothing was merged.")
        for attempt in range(MERGE_CHECK_TRIES):
            r = await api("POST", f"/repos/{owner()}/{slug}/pulls/{number}/merge",
                          json_body={"Do": "merge", "delete_branch_after_merge": True},
                          allow=(405, 409))
            if not _still_checking(r) or attempt == MERGE_CHECK_TRIES - 1:
                break
            await asyncio.sleep(MERGE_CHECK_WAIT)
        if r.status_code in (405, 409):
            now = await _pull(slug, number) or {}
            err = scrub(_merge_refusal(r, number, row.get("pr_url"), now.get("mergeable")))
            await db.execute("UPDATE git_requests SET error = ? WHERE id = ?", (err, rid))
            await db.commit()
            raise GiteaRefused(err)
        pr = await _pull(slug, number) or {}
    return await _finish(db, rid, "approved", pr.get("merge_commit_sha"),
                         await _sync_quiet(slug))


def _still_checking(r: httpx.Response) -> bool:
    """A merge 405 "Please try again later". Gitea says this both while it is
    still working out whether the PR merges (right after main moved) and, for
    good, when it does not merge because it conflicts: the API has no other
    word for "not mergeable". Seen live 2026-09-29: a conflicting PR answered
    it every 3 s for over a minute, with `mergeable: false`."""
    return r.status_code == 405 and "try again later" in _api_message(r).lower()


def _merge_refusal(r: httpx.Response, number: int, url: str | None,
                   mergeable: bool | None = None) -> str:
    """Gitea's 405/409 on a merge, in words: what it is and what to do."""
    where = f" ({url})" if url else ""
    msg = _api_message(r)
    if _still_checking(r):
        if mergeable is False:
            return (f"Gitea won't merge pull request #{number}: it reports it as not "
                    "mergeable, which after main has moved usually means it conflicts with "
                    "what was merged since the agent branched. Reject this request and ask "
                    "the agent to file a fresh one (it will be built on the new main), or "
                    f"open it in Gitea to see the conflict{where}. It stays pending.")
        return (f"Gitea hasn't finished checking whether pull request #{number} can merge. "
                f"Wait a few seconds and approve again; it stays pending{where}.")
    if r.status_code == 409:
        return (f"Gitea can't merge pull request #{number}: {msg}. Main has probably moved "
                "since the agent branched. Reject this request and ask the agent to file "
                f"a fresh one, or resolve and merge it in Gitea{where}. It stays pending.")
    return (f"Gitea won't merge pull request #{number}: {msg}. Check it in Gitea{where}; "
            "the request stays pending.")


async def reject_push(db, rid: int, row: dict) -> dict:
    slug, number, branch = row["project_slug"], row["pr_number"], row["branch"]
    if enabled():
        pr = await _pull(slug, number)
        if pr is not None:              # None: already gone from Gitea, nothing to close
            if pr.get("merged"):
                raise ValueError("the pull request was already merged in Gitea; use "
                                 "Approve to record it")
            if pr.get("state") != "closed":
                await api("PATCH", f"/repos/{owner()}/{slug}/pulls/{number}",
                          json_body={"state": "closed"})
            await _delete_branch(slug, branch)
    return await _finish(db, rid, "rejected", None, None)


async def reconcile(slug: str) -> None:
    """Pending push rows follow what happened in Gitea (merged or closed there
    by hand). Best effort: Gitea down leaves them pending."""
    if not enabled():
        return
    db = await get_db()
    try:
        async with db.execute(
                "SELECT * FROM git_requests WHERE project_slug = ? AND kind = 'push' "
                "AND status = 'pending'", (slug,)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        for row in rows:
            try:
                pr = await _pull(slug, row["pr_number"])
                if pr is None:
                    await _finish(db, row["id"], "rejected", None,
                                  "the pull request no longer exists in Gitea")
                elif pr.get("merged"):
                    await _finish(db, row["id"], "approved", pr.get("merge_commit_sha"),
                                  await _sync_quiet(slug))
                elif pr.get("state") == "closed":
                    await _delete_branch(slug, row["branch"])
                    await _finish(db, row["id"], "rejected", None, "closed in Gitea")
            except GiteaUnreachable as e:
                log.info("gitea reconcile %s: %s", slug, e)
                break               # down: one failed call is enough, the rest wait
            except (GiteaError, GiteaOff, RuntimeError) as e:
                log.info("gitea reconcile %s #%s: %s", slug, row["id"], e)
    finally:
        await db.close()


# --- the dashboard -------------------------------------------------------------

async def status() -> dict:
    out = {"enabled": bool(settings.gitea_enabled), "configured": enabled(),
           "url": public_url(), "port": settings.gitea_port, "owner": owner(),
           "bot": bot_user(), "running": False, "version": None,
           "tokens_private": all(token_file_ok(p) for p in (
               settings.gitea_admin_token_path, settings.gitea_bot_token_path))}
    if enabled():
        out["version"] = await version()
        out["running"] = out["version"] is not None
    else:
        # what stops it being on, so the panel can say more than "not set up"
        out["missing"] = [m for m, ok in (
            ("JARVIS_GITEA_ENABLED is off", bool(settings.gitea_enabled)),
            ("no owner (JARVIS_GITEA_OWNER)", bool(owner())),
            ("the operator's token file", admin_token() is not None),
            ("the agent bot's token file", bot_token() is not None)) if not ok]
    return out


async def list_repos() -> list[dict]:
    r = await api("GET", f"/users/{owner()}/repos?limit=50")
    return [{"name": x.get("name"), "private": x.get("private"),
             "url": f"{public_url()}/{owner()}/{x.get('name')}",
             "updated": x.get("updated_at")} for x in r.json()]


async def list_users() -> list[dict]:
    r = await api("GET", "/admin/users?limit=50")
    return [{"login": u.get("login"), "email": u.get("email"),
             "is_admin": u.get("is_admin"), "active": u.get("active"),
             "prohibit_login": u.get("prohibit_login"),
             "bot": u.get("login") == bot_user()} for u in r.json()]


_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}$")


def _check_login(login: str) -> str:
    if not _LOGIN.match(login or ""):
        raise ValueError("username: letters, digits, . _ - (max 39)")
    return login


async def create_user(login: str, email: str, password: str) -> dict:
    _check_login(login)
    if len(password or "") < 8:
        raise ValueError("password must be at least 8 characters")
    r = await api("POST", "/admin/users", json_body={
        "username": login, "email": email or f"{login}@localhost",
        "password": password, "must_change_password": True, "send_notify": False})
    return {"login": r.json().get("login")}


async def _edit_user(login: str, body: dict) -> None:
    _check_login(login)
    if login in (owner(), bot_user()):
        raise ValueError(f"{login} is managed by Jav3 — change it in Gitea itself")
    await api("PATCH", f"/admin/users/{login}", json_body={"login_name": login,
                                                           "source_id": 0, **body})


async def reset_password(login: str, password: str) -> None:
    if len(password or "") < 8:
        raise ValueError("password must be at least 8 characters")
    await _edit_user(login, {"password": password, "must_change_password": True})


async def set_disabled(login: str, disabled: bool) -> None:
    await _edit_user(login, {"prohibit_login": bool(disabled), "active": not disabled})


def egress_refusal(host: str, port) -> str | None:
    """The box-side egress proxy never reaches Gitea: its name (any port), or
    any loopback/host address on Gitea's port. The host's own LAN IPs are
    refused by LAN access already; this is the explicit belt."""
    h = (host or "").strip().lower().rstrip(".").strip("[]")
    names = set()
    if settings.gitea_url.strip():
        from urllib.parse import urlsplit
        try:
            n = urlsplit(settings.gitea_url.strip()).hostname
        except ValueError:
            n = None
        if n:
            names.add(n.lower())
    if h in names:
        return f"refused: {h} is the host's Gitea (agents file git_push_request instead)"
    try:
        p = int(port) if port is not None else None
    except (TypeError, ValueError):
        p = None
    if p == settings.gitea_port:
        local = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
        try:
            from .lanaccess import host_ips
            local |= set(host_ips())
        except Exception:
            pass
        if h in local or h.startswith("127."):
            return f"refused: {h}:{p} is the host's Gitea (agents file git_push_request instead)"
    return None


def token_file_ok(p: Path) -> bool:
    try:
        return (os.stat(p).st_mode & 0o077) == 0
    except OSError:
        return False
