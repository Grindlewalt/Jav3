"""GitHub remote plumbing: URL validation, token-in-env-only auth, scrubbing,
the remote API, real push/pull mechanics against a local bare repo (file://),
and import-as-project. All offline."""
import asyncio
import base64
import json

import httpx
import pytest

from backend import gitgate
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
            ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/auth/login",
                         json={"username": "operator", "password": "hunter2"})
        assert r.status_code == 200
        yield c


async def _sh(*args, cwd=None):
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    assert proc.returncode == 0, (err or out).decode()
    return out.decode()


def test_valid_remote():
    ok = gitgate.valid_remote
    assert ok("https://github.com/owner/repo")
    assert ok("https://github.com/owner/repo.git")
    assert not ok("http://github.com/owner/repo")          # no plaintext
    assert not ok("https://github.com/owner")              # needs owner/repo
    assert not ok("https://evil.com/owner/repo")
    assert not ok("https://user:pass@github.com/o/r")      # no inline creds
    assert not ok("git@github.com:owner/repo.git")
    assert not ok("file:///tmp/somewhere")


def test_auth_env_and_scrub(tmp_env):
    # no token configured -> no auth env
    assert gitgate._auth_env("https://github.com/o/r") == {}
    settings.secrets_path.write_text(json.dumps({"GITHUB_TOKEN": "tok_sekrit"}))
    env = gitgate._auth_env("https://github.com/o/r")
    b64 = base64.b64encode(b"x-access-token:tok_sekrit").decode()
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {b64}"
    # token only rides for github.com
    assert gitgate._auth_env("https://evil.com/o/r") == {}
    assert gitgate._auth_env("file:///tmp/x") == {}
    assert gitgate._auth_env(None) == {}
    # scrub removes the raw value from any surfaced text
    assert gitgate._scrub("fatal: auth tok_sekrit rejected") == "fatal: auth *** rejected"


async def _mkproject(client, name="Remote Test"):
    r = await client.post("/api/projects", json={"name": name})
    assert r.status_code == 200
    return r.json()["slug"]


async def test_remote_api_validation_and_roundtrip(client, monkeypatch):
    slug = await _mkproject(client)
    r = await client.get(f"/api/projects/{slug}/git/remote")
    assert r.status_code == 200
    assert r.json()["url"] is None

    for bad in ("http://github.com/o/r", "https://gitlab.com/o/r", "nonsense"):
        r = await client.put(f"/api/projects/{slug}/git/remote", json={"url": bad})
        assert r.status_code == 400

    async def fake_verify(slug, url):  # network check stubbed out offline
        return None
    monkeypatch.setattr(gitgate, "verify_remote", fake_verify)
    url = "https://github.com/owner/repo"
    r = await client.put(f"/api/projects/{slug}/git/remote", json={"url": url})
    assert r.status_code == 200
    r = await client.get(f"/api/projects/{slug}/git/remote")
    assert r.json()["url"] == url
    # mirrored onto the repo's origin, clean (no credentials)
    _, out, _ = await gitgate.run_git(slug, "remote", "get-url", "origin")
    assert out.strip() == url

    # disconnect
    r = await client.put(f"/api/projects/{slug}/git/remote", json={"url": None})
    assert r.status_code == 200
    r = await client.get(f"/api/projects/{slug}/git/remote")
    assert r.json()["url"] is None
    rc, _, _ = await gitgate.run_git(slug, "remote", "get-url", "origin")
    assert rc != 0


async def test_push_pull_against_local_bare(client, tmp_path):
    slug = await _mkproject(client)
    bare = tmp_path / "bare.git"
    await _sh("git", "init", "-q", "--bare", str(bare))
    # set_remote itself doesn't gate the URL (the API does) — file:// lets the
    # whole push/fetch/merge path run offline
    await gitgate.set_remote(slug, f"file://{bare}")

    d = settings.projects_dir / slug
    (d / "hello.txt").write_text("v1\n")
    await gitgate.run_git(slug, "add", "-A", check=True)
    await gitgate.run_git(slug, "commit", "-q", "-m", "v1", check=True)
    out = await gitgate.push_to_remote(slug)
    assert "error" not in out.lower()
    branch = await gitgate.current_branch(slug)
    remote_head = (await _sh("git", "-C", str(bare), "rev-parse", branch)).strip()
    local_head = (await gitgate.run_git(slug, "rev-parse", "HEAD"))[1].strip()
    assert remote_head == local_head

    ab = await gitgate.ahead_behind(slug, fetch=True)
    assert ab == {"ahead": 0, "behind": 0}

    # someone else pushes; we pull it in (ff-only)
    other = tmp_path / "other"
    await _sh("git", "clone", "-q", f"file://{bare}", str(other))
    (other / "hello.txt").write_text("v2\n")
    await _sh("git", "-C", str(other), "-c", "user.name=o", "-c", "user.email=o@x",
              "commit", "-aqm", "v2")
    await _sh("git", "-C", str(other), "push", "-q", "origin", "HEAD")

    ab = await gitgate.ahead_behind(slug, fetch=True)
    assert ab == {"ahead": 0, "behind": 1}
    # dirty tree refuses the pull
    (d / "wip.txt").write_text("uncommitted")
    with pytest.raises(ValueError, match="uncommitted"):
        await gitgate.pull_from_remote(slug)
    (d / "wip.txt").unlink()
    await gitgate.pull_from_remote(slug)
    assert (d / "hello.txt").read_text() == "v2\n"

    # push with no remote refuses cleanly
    await gitgate.set_remote(slug, None)
    with pytest.raises(ValueError, match="no remote"):
        await gitgate.push_to_remote(slug)


async def test_import_project(client, tmp_path, monkeypatch):
    src = tmp_path / "srcrepo"
    src.mkdir()
    await _sh("git", "init", "-q", str(src))
    (src / "README.md").write_text("# upstream\n")
    await _sh("git", "-C", str(src), "add", "-A")
    await _sh("git", "-C", str(src), "-c", "user.name=o", "-c", "user.email=o@x",
              "commit", "-qm", "init")
    monkeypatch.setattr(gitgate, "valid_remote", lambda u: True)

    url = f"file://{src}"
    r = await client.post("/api/projects/import", json={"url": url, "name": "srcrepo"})
    assert r.status_code == 200, r.text
    slug = r.json()["slug"]
    d = settings.projects_dir / slug
    assert (d / "README.md").read_text() == "# upstream\n"
    assert (d / ".git").is_dir()
    assert (d / "project.md").exists()          # scaffolded since upstream had none
    r = await client.get(f"/api/projects/{slug}/git/remote")
    assert r.json()["url"] == url

    # duplicate slug refused
    r = await client.post("/api/projects/import", json={"url": url, "name": "srcrepo"})
    assert r.status_code == 409


async def test_import_rejects_bad_url(client):
    r = await client.post("/api/projects/import", json={"url": "ftp://nope"})
    assert r.status_code == 400
    r = await client.post("/api/projects/import",
                          json={"url": "https://gitlab.com/o/r"})
    assert r.status_code == 400


async def test_remote_request_flow(client, tmp_path, monkeypatch):
    slug = await _mkproject(client, "Remote Req")
    # invalid URL refused at filing time (agent gets immediate feedback)
    with pytest.raises(ValueError, match="github.com"):
        await gitgate.create_remote_request(slug, "http://github.com/o/r")

    bare = tmp_path / "req-bare.git"
    await _sh("git", "init", "-q", "--bare", str(bare))
    url = f"file://{bare}"
    monkeypatch.setattr(gitgate, "valid_remote", lambda u: True)
    row = await gitgate.create_remote_request(slug, url)
    assert row["kind"] == "remote" and row["status"] == "pending"
    # only one pending remote request per project
    with pytest.raises(ValueError, match="already pending"):
        await gitgate.create_remote_request(slug, url)

    # a commit exists -> approval should connect AND push it
    d = settings.projects_dir / slug
    (d / "f.txt").write_text("x\n")
    await gitgate.run_git(slug, "add", "-A", check=True)
    await gitgate.run_git(slug, "commit", "-q", "-m", "x", check=True)

    async def fake_verify(slug, url):
        return None
    monkeypatch.setattr(gitgate, "verify_remote", fake_verify)
    r = await client.post(f"/api/projects/{slug}/git/requests/{row['id']}/approve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved" and body["error"] is None
    assert await gitgate.get_remote(slug) == url
    branch = await gitgate.current_branch(slug)
    remote_sha = (await _sh("git", "-C", str(bare), "rev-parse", branch)).strip()
    local_sha = (await gitgate.run_git(slug, "rev-parse", "HEAD"))[1].strip()
    assert remote_sha == local_sha

    # reject path works for remote kind too
    row2 = await gitgate.create_remote_request(slug, url)
    r = await client.post(f"/api/projects/{slug}/git/requests/{row2['id']}/reject")
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert await gitgate.get_remote(slug) == url   # unchanged by the reject
