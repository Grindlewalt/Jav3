"""Gitea integration (backend/gitea.py): repos, the agent's push request
lifecycle, token custody, and the operator-only Settings API. Gitea's HTTP API
is an httpx.MockTransport; its git side is a local bare repo."""
import json
import re
import subprocess

import httpx
import pytest

from backend import devicetokens, gitea
from backend.agent.tools import registry
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds

ADMIN_TOK = "a" * 40
BOT_TOK = "b" * 40


def _git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class FakeGitea:
    """Just enough of Gitea's API, keyed on the paths backend/gitea.py uses."""

    def __init__(self, bare):
        self.bare = bare
        self.repos: dict[str, dict] = {}
        self.collab: dict[str, str] = {}
        self.protect: dict[str, dict] = {}
        self.pulls: dict[int, dict] = {}
        self.users = [{"login": "operator", "email": "o@x", "is_admin": True,
                       "active": True, "prohibit_login": False},
                      {"login": "jav3-agent", "email": "b@x", "is_admin": False,
                       "active": True, "prohibit_login": False}]
        self.calls: list[tuple[str, str, str]] = []   # (method, path, token)

    def merge(self, n: int) -> None:
        pr = self.pulls[n]
        sha = _git("--git-dir", str(self.bare), "rev-parse", f"refs/heads/{pr['head']}")
        _git("--git-dir", str(self.bare), "update-ref", "refs/heads/main", sha)
        _git("--git-dir", str(self.bare), "update-ref", "-d", f"refs/heads/{pr['head']}")
        pr.update(merged=True, state="closed", merge_commit_sha=sha)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        tok = req.headers.get("authorization", "").removeprefix("token ")
        path = req.url.path.removeprefix("/api/v1")
        m = req.method
        self.calls.append((m, path, tok))
        if tok not in (ADMIN_TOK, BOT_TOK):
            return httpx.Response(401, json={"message": "bad token"})
        body = json.loads(req.content) if req.content else {}
        if path == "/version":
            return httpx.Response(200, json={"version": "1.27.3"})
        if path == "/user/repos" and m == "POST":
            self.repos[body["name"]] = {"name": body["name"], "private": body["private"]}
            return httpx.Response(201, json=self.repos[body["name"]])
        if path == "/users/operator/repos":
            return httpx.Response(200, json=list(self.repos.values()))
        if path == "/admin/users" and m == "GET":
            return httpx.Response(200, json=self.users)
        if path == "/admin/users" and m == "POST":
            self.users.append({"login": body["username"], "active": True})
            return httpx.Response(201, json={"login": body["username"]})
        if (mm := re.fullmatch(r"/admin/users/([^/]+)", path)) and m == "PATCH":
            u = next((u for u in self.users if u["login"] == mm[1]), None)
            if not u:
                return httpx.Response(404, json={})
            u.update({k: v for k, v in body.items() if k != "password"})
            return httpx.Response(200, json=u)
        mm = re.fullmatch(r"/repos/operator/([^/]+)(/.*)?", path)
        if not mm:
            return httpx.Response(404, json={})
        repo, rest = mm[1], mm[2] or ""
        if rest == "":
            return (httpx.Response(200, json=self.repos[repo]) if repo in self.repos
                    else httpx.Response(404, json={}))
        if rest.startswith("/collaborators/"):
            self.collab[rest.split("/")[-1]] = body["permission"]
            return httpx.Response(204)
        if rest == "/branch_protections/main":
            if m == "GET":
                return (httpx.Response(200, json=self.protect["main"]) if "main" in self.protect
                        else httpx.Response(404, json={}))
            self.protect["main"].update(body)
            return httpx.Response(200, json=self.protect["main"])
        if rest == "/branch_protections" and m == "POST":
            self.protect["main"] = body
            return httpx.Response(201, json=body)
        if rest == "/pulls" and m == "POST":
            if tok != BOT_TOK:
                return httpx.Response(403, json={})
            n = len(self.pulls) + 1
            self.pulls[n] = {"number": n, "head": body["head"], "base": body["base"],
                             "title": body["title"], "state": "open", "merged": False,
                             "merge_commit_sha": None}
            return httpx.Response(201, json=self.pulls[n])
        if mm2 := re.fullmatch(r"/pulls/(\d+)(/merge)?", rest):
            pr = self.pulls[int(mm2[1])]
            if mm2[2] and m == "POST":
                if tok != ADMIN_TOK:
                    return httpx.Response(403, json={})
                self.merge(pr["number"])
                return httpx.Response(200)
            if m == "PATCH":
                pr["state"] = body.get("state", pr["state"])
            return httpx.Response(200, json=pr)
        if rest.startswith("/branches/") and m == "DELETE":
            b = rest.removeprefix("/branches/")
            subprocess.run(["git", "--git-dir", str(self.bare), "update-ref", "-d",
                            f"refs/heads/{b}"], capture_output=True)
            return httpx.Response(204)
        return httpx.Response(404, json={})


@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        await c.post("/api/projects", json={"name": "Demo", "summary": "demo"})
        await c.post("/api/projects/demo/load")
        yield c


@pytest.fixture
def fake(tmp_env, monkeypatch):
    bare = tmp_env / "gitea-bare.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    (tmp_env / "admin.tok").write_text(ADMIN_TOK + "\n")
    (tmp_env / "bot.tok").write_text(BOT_TOK + "\n")
    monkeypatch.setattr(settings, "gitea_enabled", True)
    monkeypatch.setattr(settings, "gitea_owner", "operator")
    monkeypatch.setattr(settings, "gitea_url", "http://gitbox.lan:3000")
    monkeypatch.setattr(settings, "gitea_admin_token_path", tmp_env / "admin.tok")
    monkeypatch.setattr(settings, "gitea_bot_token_path", tmp_env / "bot.tok")
    f = FakeGitea(bare)
    monkeypatch.setattr(gitea, "_transport", httpx.MockTransport(f))
    monkeypatch.setattr(gitea, "repo_url", lambda slug: str(bare))
    return f


def _pdir():
    return settings.projects_dir / "demo"


def _head() -> str:
    return _git("rev-parse", "HEAD", cwd=_pdir())


def _no_tokens(*texts):
    for t in texts:
        assert ADMIN_TOK not in str(t) and BOT_TOK not in str(t)


# --- repos + naming -------------------------------------------------------------

async def test_ensure_repo(client, fake):
    await gitea.ensure_repo("demo")
    assert fake.repos["demo"]["private"] is True
    assert fake.collab == {"jav3-agent": "write"}
    rule = fake.protect["main"]
    assert rule["enable_push_whitelist"] and rule["push_whitelist_usernames"] == ["operator"]
    assert rule["merge_whitelist_usernames"] == ["operator"]
    assert "jav3-agent" not in json.dumps(rule)
    # main is up on Gitea, pushed as the operator; the remote is credential-free
    assert _git("--git-dir", str(fake.bare), "rev-parse", "main") == _head()
    cfg = (_pdir() / ".git" / "config").read_text()
    _no_tokens(cfg)
    assert "extraheader" not in cfg.lower() and "@" not in cfg.split('[remote "gitea"]')[1]
    await gitea.ensure_repo("demo")          # idempotent
    assert len(fake.repos) == 1


def test_branch_naming():
    for _ in range(20):
        assert re.fullmatch(r"agent/[0-9a-f]{8}", gitea.new_branch())
    for bad in ("main", "refs/heads/main", "agent/../main", "agent/abcd1234:main",
                "agent/ABCDEF12", "agent/abc", "HEAD", "+agent/abcd1234", None):
        with pytest.raises(ValueError):
            gitea.check_agent_ref(bad)


async def test_bot_cannot_target_main(client, fake):
    await gitea.ensure_repo("demo")
    with pytest.raises(ValueError):
        await gitea.push_agent_branch("demo", _head(), "main")
    with pytest.raises(ValueError):
        await gitea._delete_branch("demo", "main")
    # the tool exposes no branch/ref argument at all: the host names it
    spec = (settings.tools_dir / "git_push_request" / "TOOL.md").read_text()
    params = spec.split("properties:")[1].split("required:")[0]
    assert "branch" not in params and "ref" not in params


# --- the push request lifecycle ----------------------------------------------

async def _file(client, fake) -> dict:
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    base = _head()
    out = await registry.dispatch("git_push_request",
                                  {"title": "Add x", "description": "a loader"})
    assert "pending" in out and "agent/" in out and "/pulls/1" in out, out
    _no_tokens(out)
    assert _head() == base                       # host main untouched
    r = await client.get("/api/projects/demo/git/requests")
    row = r.json()["requests"][0]
    assert row["kind"] == "push" and row["status"] == "pending"
    assert re.fullmatch(r"agent/[0-9a-f]{8}", row["branch"]) and row["pr_number"] == 1
    assert row["pr_url"] == "http://gitbox.lan:3000/operator/demo/pulls/1"
    assert "code/x.py" in row["summary"]
    _no_tokens(json.dumps(row))
    # the branch holds the change on top of main; the PR was opened by the bot
    assert _git("--git-dir", str(fake.bare), "rev-parse", f"{row['branch']}^") == base
    assert ("POST", "/repos/operator/demo/pulls", BOT_TOK) in fake.calls
    return row


async def test_push_request_approve_merges(client, fake):
    row = await _file(client, fake)
    r = await client.post(f"/api/projects/demo/git/requests/{row['id']}/approve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved" and body["error"] is None
    assert fake.pulls[1]["merged"]
    # merged as the operator, never the bot
    assert ("POST", "/repos/operator/demo/pulls/1/merge", ADMIN_TOK) in fake.calls
    # host main fast-forwarded; the live file stayed and is now clean
    assert _head() == body["commit_sha"]
    assert (_pdir() / "code" / "x.py").read_text() == "print(1)\n"
    assert _git("status", "--porcelain", cwd=_pdir()) == ""


async def test_push_request_reject_closes(client, fake):
    row = await _file(client, fake)
    r = await client.post(f"/api/projects/demo/git/requests/{row['id']}/reject")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert fake.pulls[1]["state"] == "closed" and not fake.pulls[1]["merged"]
    heads = _git("--git-dir", str(fake.bare), "for-each-ref", "--format=%(refname)")
    assert row["branch"] not in heads
    assert (_pdir() / "code" / "x.py").exists()      # work kept, like a commit reject


async def test_reconcile_when_merged_in_gitea(client, fake):
    row = await _file(client, fake)
    fake.merge(1)                                     # the operator merged it in Gitea
    r = await client.get("/api/projects/demo/git/requests")
    got = r.json()["requests"][0]
    assert got["id"] == row["id"] and got["status"] == "approved"
    assert _head() == fake.pulls[1]["merge_commit_sha"]


async def test_reconcile_when_closed_in_gitea(client, fake):
    await _file(client, fake)
    fake.pulls[1]["state"] = "closed"
    r = await client.get("/api/projects/demo/git/requests")
    assert r.json()["requests"][0]["status"] == "rejected"


async def test_commit_approval_pushes_main_to_gitea(client, fake):
    await gitea.ensure_repo("demo")
    (_pdir() / "a.txt").write_text("a")
    await registry.dispatch("git_commit_request", {"message": "Add a"})
    rid = (await client.get("/api/projects/demo/git/requests")).json()["requests"][0]["id"]
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/approve")
    assert r.status_code == 200 and r.json()["error"] is None, r.text
    assert _git("--git-dir", str(fake.bare), "rev-parse", "main") == _head()


async def test_tokens_never_leak(client, fake):
    await _file(client, fake)
    _no_tokens((_pdir() / ".git" / "config").read_text())
    for p in (_pdir() / ".git").glob("*.index"):
        raise AssertionError(f"temp index left behind: {p}")
    # a failing git call surfaces scrubbed text
    assert gitea.scrub(f"fatal: {ADMIN_TOK} and {BOT_TOK}") == "fatal: *** and ***"


async def test_off_means_as_before(client, tmp_env):
    assert settings.gitea_enabled is False
    (_pdir() / "code" / "y.py").write_text("y\n")
    out = await registry.dispatch("git_push_request", {"title": "Add y"})
    assert "isn't set up" in out and "git_commit_request" in out
    r = await client.get("/api/projects/demo/git/requests")
    assert r.json()["requests"] == []
    assert (await client.get("/api/gitea/status")).json()["configured"] is False
    assert (await client.get("/api/gitea/users")).status_code == 409


# --- operator-only Settings API ------------------------------------------------

async def test_gitea_api_operator_only(client, fake):
    r = await client.get("/api/gitea/status")
    assert r.status_code == 200 and r.json()["running"] and r.json()["version"] == "1.27.3"
    _no_tokens(r.text)
    r = await client.post("/api/gitea/users",
                          json={"login": "friend", "email": "", "password": "longenough"})
    assert r.status_code == 200 and r.json()["login"] == "friend"
    assert (await client.post("/api/gitea/users/friend/disable",
                              json={"disabled": True})).status_code == 200
    assert any(u["login"] == "friend" and u["prohibit_login"] for u in fake.users)
    assert (await client.post("/api/gitea/users/friend/password",
                              json={"password": "short"})).status_code == 400
    # Jav3's own accounts are not editable from here
    assert (await client.post("/api/gitea/users/jav3-agent/disable",
                              json={"disabled": True})).status_code == 400
    users = (await client.get("/api/gitea/users")).json()["users"]
    assert {u["login"] for u in users} >= {"operator", "jav3-agent", "friend"}

    tok, _ = await devicetokens.mint("laptop", by="operator", scope="cli")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as anon:
        for method, path in (("GET", "/api/gitea/status"), ("GET", "/api/gitea/users"),
                             ("POST", "/api/gitea/users")):
            assert (await anon.request(method, path)).status_code == 401
            r = await anon.request(method, path, headers={"Authorization": f"Bearer {tok}"},
                                   json={"login": "x", "password": "longenough"})
            assert r.status_code == 401


# --- setup (dry run only: no binary, no systemd here) ----------------------------

def test_setup_dry_run_changes_nothing(tmp_env, monkeypatch, capsys):
    from backend import gitea_setup
    monkeypatch.setattr(settings, "gitea_dir", tmp_env / "gitea")
    monkeypatch.setattr(settings, "gitea_url", "http://gitbox.lan:3000")
    monkeypatch.setattr(settings, "gitea_admin_token_path", tmp_env / "admin.tok")
    monkeypatch.setattr(settings, "gitea_bot_token_path", tmp_env / "bot.tok")
    monkeypatch.setattr(gitea_setup, "ENV_FILE", tmp_env / "env")
    monkeypatch.setattr(gitea_setup.Path, "home", lambda: tmp_env)
    gitea_setup.run(["--dry-run", "--user", "operator", "--yes"])
    out = capsys.readouterr().out
    assert "would: download https://dl.gitea.com/gitea/1.27.3/" in out
    assert "would: create Gitea user operator (admin)" in out
    assert "would: create Gitea user jav3-agent" in out
    assert "JARVIS_GITEA_ENABLED" in out and "nothing was changed" in out
    assert not (tmp_env / "gitea").exists() and not (tmp_env / "env").exists()
    assert not (tmp_env / "admin.tok").exists()


def test_setup_pins_and_ini():
    from backend import gitea_setup
    assert set(gitea_setup.GITEA_SHA256) == {"amd64", "arm64"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in gitea_setup.GITEA_SHA256.values())
    assert gitea_setup.gitea_arch("x86_64") == "amd64"
    assert gitea_setup.gitea_arch("aarch64") == "arm64"
    ini = gitea_setup.app_ini(settings.base_dir, 3000, "http://h:3000", "me", "k", "t")
    for line in ("INSTALL_LOCK = true", "DISABLE_REGISTRATION = true",
                 "REQUIRE_SIGNIN_VIEW = true", "OFFLINE_MODE = true",
                 "DISABLE_SSH = true", "DB_TYPE = sqlite3", "HTTP_PORT = 3000"):
        assert line in ini
