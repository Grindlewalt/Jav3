"""The Git page's backend (backend/gitea.py, gitea_api.py): who can open a repo,
the repo list, branches, history, the agent's pull requests with a capped and
scrubbed diff, and the host's main against Gitea's.

Fixtures are test_gitea's: Gitea's API is a MockTransport, its git side a bare
repo. `Page` answers the paths this file adds (collaborators per repo, branches,
commits, a PR's .diff) and hands everything else to test_gitea's FakeGitea."""
import json
import re

import httpx
import pytest

from backend import devicetokens, doctor, gitea, gitea_setup, secrets
from backend.main import app
from tests.test_gitea import (ADMIN_TOK, BOT_TOK, _file, _git, _head, _no_tokens,  # noqa: F401
                              _pdir, client, fake)  # the fixtures ride in on this import


class Page:
    def __init__(self, base):
        self.base = base
        self.grants: dict[str, dict[str, str]] = {}       # repo -> {login: permission}
        base.users.extend([
            {"login": "friend", "email": "f@x", "is_admin": False, "active": True,
             "prohibit_login": False},
            {"login": "gone", "email": "g@x", "is_admin": False, "active": False,
             "prohibit_login": True},
            {"login": "root2", "email": "r@x", "is_admin": True, "active": True,
             "prohibit_login": False}])

    def git(self, *args) -> str:
        return _git("--git-dir", str(self.base.bare), *args)

    def _main(self) -> str | None:
        try:
            return self.git("rev-parse", "--verify", "-q", "refs/heads/main")
        except Exception:
            return None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        m = req.method
        mm = re.fullmatch(r"/repos/operator/([^/]+)(/.*)?", path)
        if mm and mm[1] in self.base.repos:
            repo, rest = mm[1], mm[2] or ""
            grants = self.grants.setdefault(repo, {})
            if rest == "/collaborators" and m == "GET":
                return httpx.Response(200, json=[{"login": l} for l in grants])
            if (c := re.fullmatch(r"/collaborators/([^/]+)", rest)) and m == "PUT":
                grants[c[1]] = json.loads(req.content)["permission"]
                return httpx.Response(204)
            if (c := re.fullmatch(r"/collaborators/([^/]+)/permission", rest)):
                return httpx.Response(200, json={"permission": grants.get(c[1], "none")})
            if rest == "/branches" and m == "GET":
                out = []
                refs = self.git("for-each-ref", "--format=%(refname:short)%09%(objectname)%09"
                                "%(subject)%09%(creatordate:iso-strict)", "refs/heads")
                for line in refs.splitlines():
                    name, sha, subj, when = line.split("\t")
                    out.append({"name": name, "protected": name == "main",
                                "commit": {"id": sha, "message": subj, "timestamp": when}})
                return httpx.Response(200, json=out)
            if rest == "/branches/main" and m == "GET":
                sha = self._main()
                return (httpx.Response(200, json={"name": "main", "commit": {"id": sha}})
                        if sha else httpx.Response(404, json={}))
            if rest == "/commits" and m == "GET":
                q = req.url.params
                tip = q.get("sha") or "main"
                try:
                    self.git("rev-parse", "--verify", "-q", f"refs/heads/{tip}")
                except Exception:
                    return httpx.Response(409, json={"message": "Git Repository is empty."})
                limit, page = int(q.get("limit", 30)), int(q.get("page", 1))
                log = self.git("log", f"refs/heads/{tip}", f"--skip={(page - 1) * limit}",
                               f"--max-count={limit + 1}", "--format=%H%x09%an%x09%aI%x09%s")
                lines = log.splitlines()
                rows = []
                for line in lines[:limit]:
                    sha, an, when, subj = line.split("\t", 3)
                    rows.append({"sha": sha, "commit": {
                        "message": subj + "\n\nbody", "author": {"name": an, "date": when}}})
                return httpx.Response(200, json=rows, headers={
                    "x-hasmore": "true" if len(lines) > limit else "false"})
            if (c := re.fullmatch(r"/pulls/(\d+)\.diff", rest)):
                pr = self.base.pulls.get(int(c[1]))
                if not pr:
                    return httpx.Response(404, json={})
                try:
                    return httpx.Response(200, text=self.git(
                        "diff", "refs/heads/main", f"refs/heads/{pr['head']}") + "\n")
                except Exception:
                    return httpx.Response(404, json={})
        return self.base(req)


@pytest.fixture
def pg(fake, monkeypatch):
    p = Page(fake)
    monkeypatch.setattr(gitea, "_transport", httpx.MockTransport(p))
    return p


def _commit(name: str, text: str = "x\n", cwd=None) -> None:
    cwd = cwd or _pdir()
    (cwd / name).write_text(text)
    _git("add", "-A", cwd=cwd)
    _git("-c", "user.name=T", "-c", "user.email=t@x", "commit", "-qm", f"add {name}", cwd=cwd)


# --- access ---------------------------------------------------------------------

async def test_a_new_repo_is_read_by_every_enabled_account(client, pg):
    await gitea.ensure_repo("demo")
    # the bot writes (branches only), friend reads; a disabled account, a site admin
    # and the owner get nothing from this
    assert pg.grants["demo"] == {"jav3-agent": "write", "friend": "read"}
    pg.grants["demo"]["friend"] = "write"           # the operator raised it
    await gitea.ensure_repo("demo")
    assert pg.grants["demo"]["friend"] == "write"   # and a re-run never lowers it


async def test_a_new_or_re_enabled_account_reads_the_existing_repos(client, pg):
    await gitea.ensure_repo("demo")
    await gitea.ensure_remote_repo("other")
    r = await client.post("/api/gitea/users",
                          json={"login": "newbie", "email": "", "password": "longenough"})
    assert r.status_code == 200 and r.json() == {"login": "newbie", "shared": 2}
    assert pg.grants["demo"]["newbie"] == pg.grants["other"]["newbie"] == "read"
    assert "gone" not in pg.grants["demo"]
    r = await client.post("/api/gitea/users/gone/disable", json={"disabled": False})
    assert r.status_code == 200 and r.json()["shared"] == 2
    assert pg.grants["other"]["gone"] == "read"
    # disabling again takes nothing away: the account just cannot sign in
    r = await client.post("/api/gitea/users/gone/disable", json={"disabled": True})
    assert r.status_code == 200 and "shared" not in r.json()
    assert pg.grants["other"]["gone"] == "read"


async def test_failing_to_share_does_not_fail_the_repo_or_the_account(client, pg, monkeypatch):
    async def boom(*a, **k):
        raise gitea.GiteaError("Gitea refused GET /admin/users (500): oops")
    monkeypatch.setattr(gitea, "list_users", boom)
    await gitea.ensure_repo("demo")                 # still created and pushed
    assert "demo" in pg.base.repos


async def test_an_account_made_while_sharing_fails_says_so(client, pg, monkeypatch):
    async def boom():
        raise gitea.GiteaError("Gitea refused GET /users/operator/repos (500): oops")
    monkeypatch.setattr(gitea, "_repo_names", boom)
    r = await client.post("/api/gitea/users", json={"login": "n2", "password": "longenough"})
    assert r.status_code == 200
    assert r.json()["login"] == "n2" and r.json()["shared"] == 0
    assert "oops" in r.json()["share_error"]


async def test_backfill_reports_then_grants(client, pg):
    pg.base.repos["demo"] = {"name": "demo", "private": True}
    pg.base.repos["other"] = {"name": "other", "private": True}
    pg.grants["demo"] = {"jav3-agent": "write", "friend": "write"}     # friend keeps write
    dry = await gitea.backfill_access(dry=True)
    assert dry["accounts"] == ["friend"]
    assert dry["missing"] == [{"repo": "other", "login": "friend"}] and dry["granted"] == []
    assert "friend" not in pg.grants.get("other", {})
    done = await gitea.backfill_access()
    assert done["granted"] == [{"repo": "other", "login": "friend"}]
    assert pg.grants["other"]["friend"] == "read" and pg.grants["demo"]["friend"] == "write"
    assert (await gitea.backfill_access())["missing"] == []             # idempotent


async def test_access_row_and_changing_a_level(client, pg):
    await gitea.ensure_repo("demo")
    r = await client.get("/api/gitea/repos/demo/access")
    assert r.status_code == 200
    rows = {a["login"]: a for a in r.json()["access"]}
    assert rows["operator"]["permission"] == "owner"
    assert rows["jav3-agent"]["permission"] == "write" and rows["jav3-agent"]["bot"]
    assert rows["friend"]["permission"] == "read"
    assert rows["gone"]["permission"] == "none" and rows["gone"]["disabled"]
    assert "root2" not in rows                       # an admin needs no grant
    _no_tokens(r.text)

    r = await client.put("/api/gitea/repos/demo/access/friend", json={"permission": "write"})
    assert r.status_code == 200 and pg.grants["demo"]["friend"] == "write"
    for login, body, code in (("friend", {"permission": "admin"}, 400),
                              ("jav3-agent", {"permission": "read"}, 400),
                              ("operator", {"permission": "read"}, 400),
                              ("nobody", {"permission": "read"}, 404)):
        r = await client.put(f"/api/gitea/repos/demo/access/{login}", json=body)
        assert r.status_code == code, (login, r.text)
    assert pg.grants["demo"]["jav3-agent"] == "write"
    assert (await client.get("/api/gitea/repos/NOT_A_SLUG/access")).status_code == 400


# --- the repo list, branches, history ----------------------------------------------

async def test_repo_list_says_what_the_page_needs(client, pg):
    await gitea.ensure_repo("demo")
    row = await _file(client, pg.base)
    r = await client.get("/api/gitea/repos")
    assert r.status_code == 200
    [repo] = r.json()["repos"]
    assert repo["slug"] == repo["name"] == "demo" and repo["default_branch"] == "main"
    assert repo["url"] == "http://gitbox.lan:3000/operator/demo"
    assert repo["last_commit"]["short"] == _head()[:7] and repo["last_commit"]["subject"]
    assert repo["open_prs"] == 1 and repo["shared_with"] == 1       # friend; not the bot
    assert row["id"]
    _no_tokens(r.text)


async def test_branches_put_main_then_agent_branches_with_their_pr(client, pg):
    await gitea.ensure_repo("demo")
    row = await _file(client, pg.base)
    r = await client.get("/api/gitea/repos/demo/branches")
    names = [b["name"] for b in r.json()["branches"]]
    assert names == ["main", row["branch"]]
    main, agent = r.json()["branches"]
    assert main["protected"] and not main["agent"] and main["pr_number"] is None
    assert agent["agent"] and agent["pr_number"] == 1 and agent["short"]


async def test_commits_page_and_say_when_more_follows(client, pg):
    await gitea.ensure_repo("demo")
    _commit("a.txt")
    _commit("b.txt")
    await gitea.push_host_main("demo")
    r = await client.get("/api/gitea/repos/demo/commits?limit=2")
    j = r.json()
    assert [c["subject"] for c in j["commits"]] == ["add b.txt", "add a.txt"]   # newest first
    assert j["more"] is True and j["branch"] == "main"
    assert j["commits"][0]["short"] == _head()[:7] and j["commits"][0]["author"]
    total = int(_git("rev-list", "--count", "HEAD", cwd=_pdir()))
    j2 = (await client.get(f"/api/gitea/repos/demo/commits?limit={total}")).json()
    assert j2["more"] is False and len(j2["commits"]) == total
    j3 = (await client.get("/api/gitea/repos/demo/commits?limit=1&page=2")).json()
    assert [c["subject"] for c in j3["commits"]] == ["add a.txt"]
    assert (await client.get("/api/gitea/repos/demo/commits?branch=../x")).status_code == 400
    assert (await client.get("/api/gitea/repos/demo/commits?branch=nope")).json()["commits"] == []
    assert (await client.get("/api/gitea/repos/nothere/branches")).status_code == 404


# --- the agent's pull requests ---------------------------------------------------------

def test_stat_line_numbers():
    assert gitea.stat_numbers(" a | 2 +-\n 3 files changed, 120 insertions(+), 4 deletions(-)") \
        == (3, 120, 4)
    assert gitea.stat_numbers(" 1 file changed, 1 insertion(+)") == (1, 1, 0)
    assert gitea.stat_numbers(" 1 file changed, 2 deletions(-)") == (1, 0, 2)
    assert gitea.stat_numbers("") == (None, None, None) == gitea.stat_numbers(None)


async def test_agent_pulls_join_the_requests_and_settle_after_a_decision(client, pg):
    row = await _file(client, pg.base)
    r = await client.get("/api/gitea/repos/demo/pulls")
    assert r.status_code == 200
    [pull] = r.json()["pulls"]
    assert pull["id"] == row["id"] and pull["pr_number"] == 1 and pull["title"] == "Add x"
    assert pull["branch"] == row["branch"] and pull["status"] == "pending"
    assert (pull["files"], pull["added"], pull["removed"]) == (1, 1, 0)
    assert pull["pr_url"] == "http://gitbox.lan:3000/operator/demo/pulls/1"
    assert r.json()["recent"] == []
    _no_tokens(r.text)
    # approving goes through the existing request route; the list then shows it decided
    a = await client.post(f"/api/projects/demo/git/requests/{row['id']}/approve")
    assert a.status_code == 200
    j = (await client.get("/api/gitea/repos/demo/pulls")).json()
    assert j["pulls"] == [] and [p["status"] for p in j["recent"]] == ["approved"]


async def test_a_pr_merged_in_gitea_is_settled_when_the_page_reads(client, pg):
    await _file(client, pg.base)
    pg.base.merge(1)
    j = (await client.get("/api/gitea/repos/demo/pulls")).json()
    assert j["pulls"] == [] and j["recent"][0]["status"] == "approved"


async def test_pull_diff_is_scrubbed_capped_and_only_for_the_agents_requests(client, pg,
                                                                              monkeypatch):
    secrets.save({"MYKEY": "s3cr3t-value-123"})
    (_pdir() / "code" / "x.py").write_text(
        f'KEY = "s3cr3t-value-123"\nTOKEN = "{ADMIN_TOK}"\n' + "pad = 1\n" * 10)
    from backend.agent.tools import registry
    out = await registry.dispatch("git_push_request", {"title": "Add keys"})
    assert "pending" in out
    r = await client.get("/api/gitea/repos/demo/pulls/1/diff")
    assert r.status_code == 200
    d = r.json()
    assert "+KEY = " in d["diff"] and "{{secret:MYKEY}}" in d["diff"]
    assert "s3cr3t-value-123" not in d["diff"] and ADMIN_TOK not in r.text
    assert "***" in d["diff"] and d["truncated"] is False
    assert d["pr_url"] == "http://gitbox.lan:3000/operator/demo/pulls/1"
    monkeypatch.setattr(gitea, "DIFF_CAP", 40)
    d = (await client.get("/api/gitea/repos/demo/pulls/1/diff")).json()
    assert d["truncated"] is True and d["diff"].endswith("[truncated]")
    assert len(d["diff"]) < 80
    # a pull request Jav3 did not file is not served, even if Gitea has it
    pg.base.pulls[9] = {"number": 9, "head": "agent/00000000", "base": "main",
                        "title": "someone else's", "state": "open", "merged": False,
                        "merge_commit_sha": None}
    assert (await client.get("/api/gitea/repos/demo/pulls/9/diff")).status_code == 404
    assert (await client.get("/api/gitea/repos/demo/pulls/77/diff")).status_code == 404


# --- the host's main against Gitea's --------------------------------------------------------

async def test_sync_status_and_push_main(client, pg, tmp_env):
    await gitea.ensure_repo("demo")
    j = (await client.get("/api/gitea/repos/demo/sync")).json()
    assert j["state"] == "in_sync" and j["host"] == j["gitea"] == _head()

    _commit("a.txt")
    j = (await client.get("/api/gitea/repos/demo/sync")).json()
    assert j["state"] == "host_ahead" and j["ahead"] == 1 and j["behind"] == 0
    r = await client.post("/api/gitea/repos/demo/push")
    assert r.status_code == 200 and r.json()["state"] == "in_sync"
    assert pg.git("rev-parse", "main") == _head()
    _no_tokens(r.text)

    # a merge in Gitea the host has not seen: Gitea's commit is not in the host's repo yet
    clone = tmp_env / "clone"
    _git("clone", "-q", str(pg.base.bare), str(clone))
    _commit("c.txt", cwd=clone)
    _git("push", "-q", "origin", "HEAD:main", cwd=clone)
    j = (await client.get("/api/gitea/repos/demo/sync")).json()
    assert j["state"] == "gitea_ahead" and j["behind"] == 1 and j["ahead"] == 0
    before = _head()
    r = await client.post("/api/gitea/repos/demo/push")       # the host catches up
    assert r.status_code == 200 and r.json()["state"] == "in_sync"
    assert _head() == pg.git("rev-parse", "main") != before

    # each side has a commit the other lacks: refused, nothing moves
    _commit("d.txt")
    assert (await client.post("/api/gitea/repos/demo/push")).status_code == 200
    gitea_main = pg.git("rev-parse", "main")
    _git("reset", "-q", "--hard", "HEAD~1", cwd=_pdir())
    _commit("e.txt")
    j = (await client.get("/api/gitea/repos/demo/sync")).json()
    assert (j["state"], j["ahead"], j["behind"]) == ("diverged", 1, 1)
    r = await client.post("/api/gitea/repos/demo/push")
    assert r.status_code == 502 and "diverged" in r.json()["detail"]
    assert pg.git("rev-parse", "main") == gitea_main and _head() != gitea_main


async def test_sync_and_push_need_a_project_and_gitea(client, pg, tmp_env):
    assert (await client.get("/api/gitea/repos/nope/sync")).status_code == 404
    assert (await client.post("/api/gitea/repos/nope/push")).status_code == 404


async def test_page_routes_are_operator_only(client, pg):
    tok, _ = await devicetokens.mint("laptop", by="operator", scope="cli")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as anon:
        for method, path in (("GET", "/api/gitea/repos"),
                             ("GET", "/api/gitea/repos/demo/branches"),
                             ("GET", "/api/gitea/repos/demo/commits"),
                             ("GET", "/api/gitea/repos/demo/pulls"),
                             ("GET", "/api/gitea/repos/demo/pulls/1/diff"),
                             ("GET", "/api/gitea/repos/demo/sync"),
                             ("POST", "/api/gitea/repos/demo/push"),
                             ("GET", "/api/gitea/repos/demo/access"),
                             ("PUT", "/api/gitea/repos/demo/access/friend")):
            assert (await anon.request(method, path)).status_code == 401, path
            r = await anon.request(method, path, headers={"Authorization": f"Bearer {tok}"},
                                   json={"permission": "write"})
            assert r.status_code == 401, path


async def test_status_says_whether_the_browser_address_is_set(client, pg, monkeypatch):
    assert (await client.get("/api/gitea/status")).json()["url_configured"] is True
    monkeypatch.setattr(gitea.settings, "gitea_url", "")
    assert (await client.get("/api/gitea/status")).json()["url_configured"] is False


# --- setup and doctor ---------------------------------------------------------------------

def test_doctor_sees_a_missing_grant_and_names_the_fix(pg, tmp_env, monkeypatch):
    pg.base.repos["demo"] = {"name": "demo", "private": True}
    pg.grants["demo"] = {"jav3-agent": "write"}
    assert doctor._gitea_gaps() == [{"repo": "demo", "login": "friend"}]
    monkeypatch.setattr(gitea_setup, "_healthy", lambda: True)
    monkeypatch.setattr(doctor, "_health", lambda port: False)
    st = next(s for s in doctor.stages() if s["id"] == "gitea")
    assert st["ok"] is False and st["optional"] and "friend" in st["detail"]
    assert st["fix"].endswith("-m backend.cli gitea-setup --access") and st["who"] == "agent"
    assert pg.grants["demo"] == {"jav3-agent": "write"}        # read-only: nothing granted
    pg.grants["demo"]["friend"] = "read"
    assert doctor._gitea_gaps() == []
    st = next(s for s in doctor.stages() if s["id"] == "gitea")
    assert st["ok"] is True and st["detail"] == "running"


def test_gitea_setup_access_flag_previews_then_grants(pg, tmp_env, monkeypatch, capsys):
    pg.base.repos["demo"] = {"name": "demo", "private": True}
    monkeypatch.setattr(gitea_setup, "_healthy", lambda: True)
    gitea_setup.run(["--access", "--dry-run"])
    out = capsys.readouterr().out
    assert "would: grant read: friend -> operator/demo" in out and "dry run" in out
    assert "friend" not in pg.grants.get("demo", {})
    gitea_setup.run(["--access"])
    out = capsys.readouterr().out
    assert "grant read: friend -> operator/demo" in out and "1 grants made for friend" in out
    assert pg.grants["demo"]["friend"] == "read"
    gitea_setup.run(["--access"])
    assert "friend can read all 1 repos" in capsys.readouterr().out
    _no_tokens(out)


def test_gitea_setup_access_flag_needs_a_running_gitea(tmp_env, monkeypatch):
    with pytest.raises(SystemExit) as e:
        gitea_setup.run(["--access"])                  # gitea off in the default test env
    assert "not set up" in str(e.value)
