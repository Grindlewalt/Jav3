"""Harness state stays out of the project's commits: .plan.json, .todo-archive.md and
the runs/<job>/*.md rollups join .workspace.json and .todo.md in the managed block of
the project's .gitignore, and a harness file that is ALREADY tracked (an old commit took
it in) is left out of the commit and reported once, never untracked behind the
operator's back. Seen in the benchmark-game run: a 238 KB .plan.json and the research
transcripts went into commits made through git_commit_request."""
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


async def _approve(client):
    reqs = (await client.get("/api/projects/demo/git/requests")).json()["requests"]
    rid = [r for r in reqs if r["status"] == "pending"][0]["id"]
    r = await client.post(f"/api/projects/demo/git/requests/{rid}/approve")
    assert r.status_code == 200, r.text
    return r.json()


async def _committed(rev="HEAD"):
    _, out, _ = await gitgate.run_git("demo", "ls-tree", "-r", "--name-only", rev, check=True)
    return out.split()


def _write_harness_files():
    (_pdir() / ".plan.json").write_text('{"items": []}')
    (_pdir() / ".todo-archive.md").write_text("# Todo archive\n")
    (_pdir() / ".workspace.json").write_text("{}")
    (_pdir() / "runs" / "abc123").mkdir(parents=True, exist_ok=True)
    (_pdir() / "runs" / "abc123" / "654-head.md").write_text("rollup\n")


# --- the managed block -------------------------------------------------------

def test_a_new_gitignore_is_the_managed_block(tmp_env):
    (settings.projects_dir / "p").mkdir(parents=True)
    assert gitgate.ensure_gitignore("p") is True
    text = (settings.projects_dir / "p" / ".gitignore").read_text()
    assert text == gitgate.GITIGNORE
    lines = text.splitlines()
    assert lines[0].startswith("# >>> Jav3 harness files") and lines[-1].startswith("# <<< Jav3 harness files")
    for want in (".plan.json", ".workspace.json", ".todo.md", ".todo-archive.md", "runs/*/*.md"):
        assert any(want in l for l in lines), want


def test_the_block_is_added_after_the_projects_own_rules_and_only_once(tmp_env):
    d = settings.projects_dir / "p"
    d.mkdir(parents=True)
    (d / ".gitignore").write_text("node_modules/\n*.log")          # no trailing newline
    assert gitgate.ensure_gitignore("p") is True
    first = (d / ".gitignore").read_text()
    assert first.startswith("node_modules/\n*.log\n\n# >>> Jav3 harness files")
    assert gitgate.ensure_gitignore("p") is False                  # idempotent: nothing to write
    assert gitgate.ensure_gitignore("p") is False
    assert (d / ".gitignore").read_text() == first
    assert first.count("# >>> Jav3 harness files") == 1


def test_an_older_block_is_replaced_in_place(tmp_env):
    d = settings.projects_dir / "p"
    d.mkdir(parents=True)
    (d / ".gitignore").write_text(
        "before/\n# >>> Jav3 harness files: an older wording\n.staging/\n# <<< Jav3 harness files\nafter/\n")
    assert gitgate.ensure_gitignore("p") is True
    text = (d / ".gitignore").read_text()
    assert text.startswith("before/\n# >>> Jav3 harness files: managed by Jav3")
    assert text.endswith("# <<< Jav3 harness files\nafter/\n")
    assert ".plan.json" in text and "an older wording" not in text


def test_a_project_without_a_directory_just_goes_without(tmp_env):
    assert gitgate.ensure_gitignore("nowhere") is False
    assert not (settings.projects_dir / "nowhere").exists()


async def test_loading_a_project_ensures_the_block(client):
    (_pdir() / ".gitignore").write_text("*.log\n")                 # an old project: no block yet
    await client.post("/api/projects/demo/load")
    text = (_pdir() / ".gitignore").read_text()
    assert text.startswith("*.log\n") and ".plan.json" in text and ".todo-archive.md" in text


# --- what a commit stages ----------------------------------------------------

async def test_harness_files_never_ride_a_commit(client):
    _write_harness_files()
    (_pdir() / "keep.txt").write_text("k\n")
    await registry.dispatch("git_commit_request", {"message": "add keep"})
    await _approve(client)
    names = await _committed()
    assert "keep.txt" in names
    assert not [n for n in names if n in (".plan.json", ".todo-archive.md", ".workspace.json")
                or n.startswith("runs/")], names


async def test_they_stay_out_even_when_the_agent_replaced_the_gitignore(client):
    (_pdir() / ".gitignore").write_text("*.pyc\n")                 # the agent's, no block
    await gitgate.ensure_repo("demo")     # puts the block back; info/exclude is the second wall
    (_pdir() / ".gitignore").write_text("*.pyc\n")
    _write_harness_files()
    (_pdir() / "keep.txt").write_text("k\n")
    await registry.dispatch("git_commit_request", {"message": "add keep"})
    await _approve(client)
    names = await _committed()
    assert "keep.txt" in names and ".plan.json" not in names
    assert not any(n.startswith("runs/") for n in names)


async def test_a_projects_own_runs_data_can_still_be_committed(client):
    _write_harness_files()
    (_pdir() / "runs").mkdir(exist_ok=True)
    (_pdir() / "runs" / "results.csv").write_text("a,b\n")
    (_pdir() / "runs" / "exp1").mkdir()
    (_pdir() / "runs" / "exp1" / "metrics.json").write_text("{}")
    await registry.dispatch("git_commit_request", {"message": "results"})
    await _approve(client)
    names = await _committed()
    assert "runs/results.csv" in names and "runs/exp1/metrics.json" in names
    assert "runs/abc123/654-head.md" not in names


# --- a harness file that is already tracked ------------------------------------

async def _track_old_harness_files():
    """What an old project looks like: .workspace.json and a rollup in history."""
    _write_harness_files()
    await gitgate.run_git("demo", "add", "-f", ".workspace.json", "runs/abc123/654-head.md",
                          check=True)
    await gitgate.run_git("demo", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                          "-m", "old", check=True)
    assert ".workspace.json" in await _committed()


async def test_a_tracked_harness_file_is_not_staged_and_reported_once(client):
    await _track_old_harness_files()
    (_pdir() / ".workspace.json").write_text('{"changed": true}')
    (_pdir() / "runs" / "abc123" / "654-head.md").write_text("rollup, grown\n")
    (_pdir() / "keep.txt").write_text("k\n")

    out = await registry.dispatch("git_commit_request", {"message": "add keep"})
    assert ".workspace.json is tracked; run `git rm --cached .workspace.json` to stop committing it" in out
    assert "runs/abc123/654-head.md is tracked; run `git rm --cached runs/abc123/654-head.md`" in out
    await _approve(client)
    # the commit took keep.txt and nothing of the harness files' new content
    _, changed, _ = await gitgate.run_git("demo", "show", "--name-only", "--format=", "HEAD", check=True)
    assert changed.split() == ["keep.txt"]
    _, shown, _ = await gitgate.run_git("demo", "show", "HEAD~1:.workspace.json", check=True)
    assert shown == "{}"
    # ...and nothing was untracked behind the operator's back
    assert ".workspace.json" in await _committed()

    (_pdir() / "more.txt").write_text("m\n")
    again = await registry.dispatch("git_commit_request", {"message": "more"})
    assert "is tracked" not in again, "said once, not on every request"
    await _approve(client)
    _, changed, _ = await gitgate.run_git("demo", "show", "--name-only", "--format=", "HEAD", check=True)
    assert changed.split() == ["more.txt"]


async def test_only_tracked_harness_changes_is_nothing_to_commit(client):
    await _track_old_harness_files()
    (_pdir() / ".workspace.json").write_text('{"changed": true}')
    out = await registry.dispatch("git_commit_request", {"message": "state"})
    assert out.startswith("error: nothing to commit")


async def test_the_push_snapshot_leaves_a_tracked_harness_file_as_head_has_it(client):
    await _track_old_harness_files()
    (_pdir() / ".workspace.json").write_text('{"changed": true}')
    (_pdir() / "keep.txt").write_text("k\n")
    sha = await gitea._snapshot_commit("demo", "snap")
    _, ws, _ = await gitgate.run_git("demo", "show", f"{sha}:.workspace.json", check=True)
    assert ws == "{}"
    assert "keep.txt" in await _committed(sha)
