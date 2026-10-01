"""BUILD-08: a new commit request replaces the project's pending one and says so.
FX1 notes: the harness's own files (.todo.md, data/) stay out of every commit even
when the agent replaced the host-written .gitignore."""
import httpx
import pytest

from backend import gitea, gitgate
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
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        await c.post("/api/projects", json={"name": "Demo", "summary": "demo"})
        await c.post("/api/projects/demo/load")
        yield c


def _pdir():
    return settings.projects_dir / "demo"


async def _requests(client):
    return (await client.get("/api/projects/demo/git/requests")).json()["requests"]


async def test_a_new_commit_request_replaces_the_pending_one_and_says_so(client):
    (_pdir() / "a.txt").write_text("a\n")
    first = await registry.dispatch("git_commit_request", {"message": "first"})
    assert "replaces" not in first
    (_pdir() / "b.txt").write_text("b\n")
    second = await registry.dispatch("git_commit_request", {"message": "second"})
    assert "replaces pending request #" in second and "first" in second
    pending = [r for r in await _requests(client) if r["status"] == "pending"]
    assert [r["message"] for r in pending] == ["second"]
    old = [r for r in await _requests(client) if r["message"] == "first"][0]
    assert old["status"] == "rejected" and "replaced by" in old["error"]


async def test_a_pending_remote_request_is_not_replaced_by_a_commit_request(client):
    (_pdir() / "a.txt").write_text("a\n")
    await gitgate.create_remote_request("demo", "https://github.com/o/r")
    await registry.dispatch("git_commit_request", {"message": "commit"})
    kinds = sorted(r["kind"] for r in await _requests(client) if r["status"] == "pending")
    assert kinds == ["commit", "remote"]


async def test_runtime_files_never_ride_a_commit_even_with_a_replaced_gitignore(client):
    (_pdir() / ".gitignore").write_text("*.pyc\n")          # the agent's, not the host's
    await gitgate.ensure_repo("demo")
    (_pdir() / "keep.txt").write_text("k\n")
    (_pdir() / ".todo.md").write_text("# Todo\n\n- [ ] x\n")
    (_pdir() / ".workspace.json").write_text("{}")
    (_pdir() / "data").mkdir(exist_ok=True)
    (_pdir() / "data" / "db.sqlite").write_text("state")
    await registry.dispatch("git_commit_request", {"message": "add keep"})
    rid = (await _requests(client))[0]["id"]
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/approve")
    assert r.status_code == 200, r.text
    _, files, _ = await gitgate.run_git("demo", "ls-files")
    tracked = files.split()
    assert "keep.txt" in tracked
    assert ".todo.md" not in tracked and ".workspace.json" not in tracked
    assert not any(t.startswith("data/") for t in tracked)


async def test_the_push_snapshot_leaves_out_the_same_files_with_the_host_gitignore(client):
    # the normal case: the host's .gitignore is in place AND the runtime files exist
    # (a pathspec-based exclude made `git add` fail here)
    await gitgate.ensure_repo("demo")
    (_pdir() / "keep.txt").write_text("k\n")
    (_pdir() / ".workspace.json").write_text("{}")
    (_pdir() / ".todo.md").write_text("x")
    (_pdir() / "data").mkdir(exist_ok=True)
    (_pdir() / "data" / "db").write_text("s")
    sha = await gitea._snapshot_commit("demo", "snap")
    _, names, _ = await gitgate.run_git("demo", "ls-tree", "-r", "--name-only", sha, check=True)
    assert "keep.txt" in names.split()
    assert not any(n in names.split() for n in (".workspace.json", ".todo.md", "data/db"))
