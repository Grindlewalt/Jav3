"""The agent -> Gitea push flow, exercised end to end against the live Pi on
2026-09-29 (a real agent turn), and the bugs that turn found:

- a guest-run turn keeps write_file/edit_file in the VM until its final message,
  so git_status said "clean", git_commit_request said "nothing to commit", and
  git_push_request opened a PR that held one host-side journal line and none of
  the agent's files;
- the tool's reply did not say what the PR changed or what happens next;
- the same changes could be filed twice;
- Gitea down, a refused merge, a deleted PR and a wrong-project request id came
  back as raw JSON, a bare 500, or a quoted KeyError.
Fixtures are test_gitea's: Gitea's API is a MockTransport, its git side a bare repo."""
import contextlib
import re

import httpx
import pytest

from backend import gitea, gitgate
from backend.agent import budget
from backend.agent.tools import registry
from backend.config import settings
from backend.vm import broker, guest_turn
from tests.test_gitea import (ADMIN_TOK, _git, _head, _no_tokens, _pdir,  # noqa: F401
                              client, fake)  # the fixtures ride in on this import


@contextlib.contextmanager
def in_guest_turn(monkeypatch, staged: dict[str, str]):
    """Pretend the next tool call is brokered from a guest turn whose write
    buffer holds `staged`: pull_writes lands it host-side, like apply_guest_writes."""
    calls: list[str] = []

    async def pull(slug):
        calls.append(slug)
        for rel, data in staged.items():
            p = _pdir() / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(data)

    monkeypatch.setattr(guest_turn, "pull_writes", pull)
    monkeypatch.setattr(broker, "get_turn", lambda op: object() if op == "op-f8" else None)
    tok = budget.active_op_id.set("op-f8")
    try:
        yield calls
    finally:
        budget.active_op_id.reset(tok)


def _wrap(monkeypatch, fake, handler):
    """Answer some Gitea API paths differently; the rest goes to the fake."""
    def call(req: httpx.Request) -> httpx.Response:
        return handler(req) or fake(req)
    monkeypatch.setattr(gitea, "_transport", httpx.MockTransport(call))


def _rows(client):
    return client.get("/api/projects/demo/git/requests")


# --- this turn's writes reach the host before a git tool looks --------------------

async def test_push_request_includes_this_turns_writes(client, fake, monkeypatch):
    staged = {"README.md": "# demo\n", "hello.py": "print('hi')\n"}
    with in_guest_turn(monkeypatch, staged) as calls:
        # (without the flush the host saw nothing here: "clean", as the live agent did)
        out = await registry.dispatch("git_push_request", {"title": "Add README and hello"})
    assert calls, "the guest's buffer was never pulled"
    assert "error" not in out.lower().split("\n")[0], out
    # the PR holds BOTH files, not only host-side edits
    row = (await _rows(client)).json()["requests"][0]
    assert "README.md" in row["summary"] and "hello.py" in row["summary"]
    tree = _git("--git-dir", str(fake.bare), "ls-tree", "-r", "--name-only", row["branch"])
    assert "README.md" in tree and "hello.py" in tree
    # and the reply lists them, so the agent can see whether its files are in
    assert "2 file(s)" in out and "A README.md" in out and "A hello.py" in out
    _no_tokens(out)


async def test_git_status_diff_and_commit_request_see_this_turns_writes(client, fake, monkeypatch):
    with in_guest_turn(monkeypatch, {"notes.txt": "x\n"}):
        status = await registry.dispatch("git_status", {})
        assert "notes.txt" in status and "clean" not in status
        assert "notes.txt" in await registry.dispatch("git_diff", {})
        out = await registry.dispatch("git_commit_request", {"message": "Add notes"})
    assert "filed" in out and "nothing to commit" not in out


async def test_flush_is_a_noop_outside_a_guest_turn(client, fake, monkeypatch):
    calls = []

    async def pull(slug):
        calls.append(slug)

    monkeypatch.setattr(guest_turn, "pull_writes", pull)
    await gitgate.flush_guest_writes("demo")            # HTTP / host-run turn: no op id
    tok = budget.active_op_id.set("op-not-brokered")    # an op with no broker envelope
    try:
        await gitgate.flush_guest_writes("demo")
    finally:
        budget.active_op_id.reset(tok)
    assert calls == []


async def test_a_failed_pull_does_not_break_the_git_tool(client, fake, monkeypatch):
    async def boom(slug):
        raise OSError("guest gone")

    monkeypatch.setattr(guest_turn, "pull_writes", boom)
    monkeypatch.setattr(broker, "get_turn", lambda op: object())
    tok = budget.active_op_id.set("op-f8")
    try:
        out = await registry.dispatch("git_status", {})
    finally:
        budget.active_op_id.reset(tok)
    assert "branch:" in out                              # answered from what the host has


# --- what the model is told ----------------------------------------------------

async def test_reply_says_what_happened_and_what_next(client, fake):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    out = await registry.dispatch("git_push_request", {"title": "Add x"})
    assert "push request #" in out and "pull request #1" in out
    assert "A code/x.py" in out and "1 file(s)" in out
    assert "pending" in out and "Don't file it again" in out
    assert "won't see the outcome" in out
    _no_tokens(out)


async def test_nothing_to_push_says_so_and_files_nothing(client, fake):
    out = await registry.dispatch("git_push_request", {"title": "Nothing"})
    assert out.startswith("error: nothing to push")
    assert "Write or edit a file" in out
    assert (await _rows(client)).json()["requests"] == []
    assert fake.pulls == {}


async def test_not_set_up_says_nothing_was_filed(client, tmp_env):
    out = await registry.dispatch("git_push_request", {"title": "x"})
    assert "isn't set up" in out and "Nothing was filed" in out and "git_commit_request" in out


# --- duplicates ---------------------------------------------------------------

async def test_same_changes_are_not_filed_twice(client, fake):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    first = await registry.dispatch("git_push_request", {"title": "Add x"})
    assert "filed" in first
    again = await registry.dispatch("git_push_request", {"title": "Add x, again"})
    assert again.startswith("error:") and "already holds exactly these changes" in again
    assert "push request #" in again and "pull request #1" in again
    assert len(fake.pulls) == 1
    rows = (await _rows(client)).json()["requests"]
    assert len(rows) == 1
    heads = _git("--git-dir", str(fake.bare), "for-each-ref", "--format=%(refname)")
    assert heads.count("refs/heads/agent/") == 1        # no stray second branch


async def test_changed_files_file_a_second_request_that_names_the_first(client, fake):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    await registry.dispatch("git_push_request", {"title": "Add x"})
    (_pdir() / "code" / "y.py").write_text("print(2)\n")
    out = await registry.dispatch("git_push_request", {"title": "Add x and y"})
    assert "filed" in out and "Earlier push request" in out and "close the others" in out
    rows = (await _rows(client)).json()["requests"]
    assert len(rows) == 2 and len(fake.pulls) == 2
    # the second holds the first's change too
    assert "code/x.py" in out and "code/y.py" in out


async def test_a_merged_request_is_settled_before_the_next_is_filed(client, fake):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    await registry.dispatch("git_push_request", {"title": "Add x"})
    fake.merge(1)                                        # the operator merged it in Gitea
    (_pdir() / "code" / "y.py").write_text("print(2)\n")
    out = await registry.dispatch("git_push_request", {"title": "Add y"})
    assert "filed" in out and "Earlier push request" not in out
    assert "1 file(s)" in out and "code/y.py" in out    # on top of the merged main
    rows = (await _rows(client)).json()["requests"]
    assert {r["status"] for r in rows} == {"approved", "pending"}


# --- runtime files never ride along ---------------------------------------------

async def test_snapshot_leaves_runtime_files_out(client, fake):
    (_pdir() / ".gitignore").write_text("")              # the agent wiped the host's ignore list
    (_pdir() / ".staging").mkdir(exist_ok=True)
    (_pdir() / ".staging" / "buffered.txt").write_text("b")
    (_pdir() / ".workspace.json").write_text("{}")
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    out = await registry.dispatch("git_push_request", {"title": "Add x"})
    assert "code/x.py" in out
    assert ".staging" not in out and ".workspace.json" not in out


# --- Gitea down --------------------------------------------------------------

def _down(monkeypatch):
    def refuse(req):
        raise httpx.ConnectError("All connection attempts failed", request=req)
    monkeypatch.setattr(gitea, "_transport", httpx.MockTransport(refuse))


async def test_gitea_down_the_tool_says_so_and_files_nothing(client, fake, monkeypatch):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    _down(monkeypatch)
    out = await registry.dispatch("git_push_request", {"title": "Add x"})
    assert out.startswith("error: Gitea isn't answering")
    assert "No pull request was made" in out and "files are safe" in out
    assert "git_commit_request" in out and "Don't retry" in out
    assert (await _rows(client)).json()["requests"] == []
    _no_tokens(out)


async def test_gitea_down_approve_and_reject_keep_the_request_pending(client, fake, monkeypatch):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    await registry.dispatch("git_push_request", {"title": "Add x"})
    rid = (await _rows(client)).json()["requests"][0]["id"]
    _down(monkeypatch)
    for verb in ("approve", "reject"):
        r = await client.post(f"/api/projects/demo/git/requests/{rid}/{verb}")
        assert r.status_code == 502, r.text
        d = r.json()["detail"]
        assert "isn't answering" in d and "still pending" in d and verb in d
        _no_tokens(d)
    got = (await _rows(client)).json()["requests"]      # listing still works, Gitea down
    assert got[0]["status"] == "pending"


# --- Gitea says no -------------------------------------------------------------

@pytest.mark.parametrize("code,words", [(409, "Gitea can't merge pull request #1"),
                                        (405, "Gitea won't merge pull request #1")])
async def test_refused_merge_is_explained_and_stays_pending(client, fake, monkeypatch, code, words):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    await registry.dispatch("git_push_request", {"title": "Add x"})
    rid = (await _rows(client)).json()["requests"][0]["id"]
    _wrap(monkeypatch, fake, lambda req: httpx.Response(code, json={"message": "Merge conflict"})
          if req.url.path.endswith("/merge") else None)
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/approve")
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert words in d and "Merge conflict" in d and "gitbox.lan:3000/operator/demo/pulls/1" in d
    assert "pending" in d and "{" not in d               # not the raw JSON
    row = (await _rows(client)).json()["requests"][0]
    assert row["status"] == "pending" and "Merge conflict" in row["error"]


async def test_pr_creation_refused_deletes_the_branch_and_reads_plainly(client, fake, monkeypatch):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    _wrap(monkeypatch, fake, lambda req: httpx.Response(
        422, json={"message": "No commits between main and the branch", "url": "http://x"})
          if req.method == "POST" and req.url.path.endswith("/pulls") else None)
    out = await registry.dispatch("git_push_request", {"title": "Add x"})
    assert "could not open the pull request" in out and "No commits between" in out
    assert '"message"' not in out and "Nothing was filed" in out
    heads = _git("--git-dir", str(fake.bare), "for-each-ref", "--format=%(refname)")
    assert "refs/heads/agent/" not in heads
    assert (await _rows(client)).json()["requests"] == []


async def test_a_pr_with_no_request_row_is_closed(client, fake, monkeypatch):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")

    async def boom(db, rid):
        raise OSError("disk full")

    monkeypatch.setattr(gitgate, "_fetch_request", boom)
    with pytest.raises(OSError):
        await gitea.create_push_request("demo", "Add x")
    assert fake.pulls[1]["state"] == "closed"            # not left open, unseen
    heads = _git("--git-dir", str(fake.bare), "for-each-ref", "--format=%(refname)")
    assert "refs/heads/agent/" not in heads


async def test_pr_deleted_in_gitea_closes_the_request(client, fake, monkeypatch):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    await registry.dispatch("git_push_request", {"title": "Add x"})
    rid = (await _rows(client)).json()["requests"][0]["id"]
    _wrap(monkeypatch, fake, lambda req: httpx.Response(404, json={"message": "not found"})
          if req.method == "GET" and re.search(r"/pulls/\d+$", req.url.path) else None)
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/approve")
    assert r.status_code == 409 and "no longer exists in Gitea" in r.json()["detail"]
    assert "Nothing was merged" in r.json()["detail"]
    row = (await _rows(client)).json()["requests"][0]
    assert row["status"] == "rejected" and "no longer exists" in row["error"]


async def test_reconcile_closes_a_request_whose_pr_vanished(client, fake, monkeypatch):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    await registry.dispatch("git_push_request", {"title": "Add x"})
    _wrap(monkeypatch, fake, lambda req: httpx.Response(404, json={})
          if req.method == "GET" and re.search(r"/pulls/\d+$", req.url.path) else None)
    row = (await _rows(client)).json()["requests"][0]
    assert row["status"] == "rejected"


async def test_reject_of_an_already_closed_pr_still_records(client, fake):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    await registry.dispatch("git_push_request", {"title": "Add x"})
    rid = (await _rows(client)).json()["requests"][0]["id"]
    fake.pulls[1]["state"] = "closed"                    # closed in Gitea by hand
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/reject")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert not [c for c in fake.calls if c[0] == "PATCH" and c[1].endswith("/pulls/1")]


# --- messages from git itself ----------------------------------------------------

def test_git_failures_read_as_what_they_are():
    e = gitea.explain_git_failure("remote: Gitea: Not allowed to push to protected branch main")
    assert "protected" in str(e) and type(e) is gitea.GiteaError
    e = gitea.explain_git_failure("fatal: unable to access 'http://127.0.0.1:3000/x.git/': "
                                  "Failed to connect to 127.0.0.1 port 3000: Connection refused")
    assert isinstance(e, gitea.GiteaUnreachable) and "isn't answering" in str(e)
    e = gitea.explain_git_failure("remote: Invalid username or password.\nfatal: Authentication failed")
    assert "token" in str(e) and "gitea-setup" in str(e) and "agent bot" in str(e)
    e = gitea.explain_git_failure("fatal: unable to access '...': The requested URL returned error: 403")
    assert "token" in str(e)
    assert str(gitea.explain_git_failure("some new git message")) == "some new git message"


async def test_protected_main_refusal_is_reported_plainly(client, fake):
    await gitea.ensure_repo("demo")
    hook = fake.bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'remote: Gitea: Not allowed to push to protected branch main' >&2\nexit 1\n")
    hook.chmod(0o755)
    (_pdir() / "a.txt").write_text("a")
    _git("add", "-A", cwd=_pdir())
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "local", cwd=_pdir())
    err = await gitea.push_main("demo")
    assert err.startswith("push of main to Gitea failed:") and "protected" in err
    # and the bot still cannot even try: the tool has no way to name main
    with pytest.raises(ValueError):
        await gitea.push_agent_branch("demo", _head(), "main")


async def test_bot_push_rejected_by_gitea_reaches_the_agent_plainly(client, fake, monkeypatch):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")

    async def refuse(slug, sha, branch):
        raise gitea.explain_git_failure("remote: Invalid username or password.\n"
                                        "fatal: Authentication failed for 'http://127.0.0.1:3000/'")

    monkeypatch.setattr(gitea, "push_agent_branch", refuse)
    out = await registry.dispatch("git_push_request", {"title": "Add x"})
    assert "could not open the pull request" in out and "gitea-setup" in out
    assert "Nothing was filed" in out
    assert fake.pulls == {}


# --- the operator API -------------------------------------------------------------

async def test_unknown_request_and_wrong_project_are_404_without_quotes(client, fake):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    await registry.dispatch("git_push_request", {"title": "Add x"})
    rid = (await _rows(client)).json()["requests"][0]["id"]
    r = await client.post("/api/projects/demo/git/requests/9999/approve")
    assert r.status_code == 404 and r.json()["detail"] == "no git request #9999"
    await client.post("/api/projects", json={"name": "Other", "summary": "o"})
    for verb in ("approve", "reject"):
        r = await client.post(f"/api/projects/other/git/requests/{rid}/{verb}")
        assert r.status_code == 404 and "in project other" in r.json()["detail"]
    assert fake.pulls[1]["state"] == "open"              # the other project's URL did nothing
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/approve")
    assert r.status_code == 200 and r.json()["status"] == "approved"


async def test_status_says_what_is_missing(client, tmp_env, monkeypatch):
    st = (await client.get("/api/gitea/status")).json()
    assert st["configured"] is False
    assert "JARVIS_GITEA_ENABLED is off" in st["missing"]
    assert "the operator's token file" in st["missing"]
    monkeypatch.setattr(settings, "gitea_enabled", True)
    monkeypatch.setattr(settings, "gitea_owner", "operator")
    (tmp_env / "admin.tok").write_text(ADMIN_TOK + "\n")
    monkeypatch.setattr(settings, "gitea_admin_token_path", tmp_env / "admin.tok")
    monkeypatch.setattr(settings, "gitea_bot_token_path", tmp_env / "no-such.tok")
    st = (await client.get("/api/gitea/status")).json()
    assert st["configured"] is False and st["missing"] == ["the agent bot's token file"]


async def test_status_configured_has_no_missing_list(client, fake):
    st = (await client.get("/api/gitea/status")).json()
    assert st["configured"] and st["running"] and "missing" not in st


# --- "Please try again later": Gitea's word for both "checking" and "conflicts" ---------

def _try_later(counter: list, until: int | None = None):
    """A merge that answers 405 "Please try again later" (until the Nth call, or always)."""
    def handler(req):
        if req.method == "POST" and req.url.path.endswith("/merge"):
            counter.append(1)
            if until is None or len(counter) <= until:
                return httpx.Response(405, json={"message": "Please try again later"})
    return handler


async def test_a_conflicting_pr_is_called_a_conflict_after_the_retries(client, fake, monkeypatch):
    monkeypatch.setattr(gitea, "MERGE_CHECK_WAIT", 0)
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    await registry.dispatch("git_push_request", {"title": "Add x"})
    rid = (await _rows(client)).json()["requests"][0]["id"]
    fake.pulls[1]["mergeable"] = False                   # what real Gitea reports for a conflict
    seen: list = []
    _wrap(monkeypatch, fake, _try_later(seen))
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/approve")
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert "not mergeable" in d and "conflicts with what was merged" in d
    assert "try again later" not in d.lower() and "file a fresh one" in d
    assert len(seen) == gitea.MERGE_CHECK_TRIES          # retried, then gave up
    assert (await _rows(client)).json()["requests"][0]["status"] == "pending"


async def test_a_pr_gitea_is_still_checking_says_wait(client, fake, monkeypatch):
    monkeypatch.setattr(gitea, "MERGE_CHECK_WAIT", 0)
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    await registry.dispatch("git_push_request", {"title": "Add x"})
    rid = (await _rows(client)).json()["requests"][0]["id"]
    _wrap(monkeypatch, fake, _try_later([]))
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/approve")
    assert r.status_code == 409
    assert "hasn't finished checking" in r.json()["detail"] and "approve again" in r.json()["detail"]


async def test_a_merge_that_clears_while_retrying_goes_through(client, fake, monkeypatch):
    monkeypatch.setattr(gitea, "MERGE_CHECK_WAIT", 0)
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    await registry.dispatch("git_push_request", {"title": "Add x"})
    rid = (await _rows(client)).json()["requests"][0]["id"]
    seen: list = []
    _wrap(monkeypatch, fake, _try_later(seen, until=1))   # checking on the first call only
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/approve")
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    assert len(seen) == 2 and fake.pulls[1]["merged"]


def test_protected_branch_refusal_is_one_readable_line():
    raw = ("remote: \nremote: error:        \nremote: error: Not allowed to push to protected "
           "branch main        \nremote: error:        \nTo http://127.0.0.1:3000/o/r.git\n"
           " ! [remote rejected] abc -> main (pre-receive hook declined)\n"
           "error: failed to push some refs to 'http://127.0.0.1:3000/o/r.git'")
    msg = str(gitea.explain_git_failure(raw))
    assert "Not allowed to push to protected branch main" in msg
    assert "\n" not in msg and "To http" not in msg and "failed to push" not in msg
    assert msg.endswith("Only the operator can push to main.")
