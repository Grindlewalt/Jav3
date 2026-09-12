"""Jav3 serves each project's repo read-only over git smart-HTTP so the operator
can `git clone/pull http://<host>/git/<slug>` over the LAN. Basic-auth gated,
pull-only (upload-pack), path-safe."""
import subprocess

import httpx
import pytest

from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app

AUTH = ("operator", "hunter2")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True)


@pytest.fixture
async def client(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    # a project on disk that is a real git repo with one commit
    proj = settings.projects_dir / "demo"
    proj.mkdir(parents=True)
    (proj / "project.md").write_text("# demo\n")
    (proj / "hello.txt").write_text("hi from demo\n")
    _git(proj, "init", "-q")
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_info_refs_requires_auth(client):
    r = await client.get("/git/demo/info/refs", params={"service": "git-upload-pack"})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("basic")


async def test_info_refs_advertises_upload_pack(client):
    r = await client.get("/git/demo/info/refs",
                         params={"service": "git-upload-pack"}, auth=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-git-upload-pack-advertisement"
    # the smart-HTTP prelude, then the ref advertisement
    assert r.content.startswith(b"001e# service=git-upload-pack\n0000")
    assert b"refs/heads/" in r.content


async def test_push_is_refused(client):
    r = await client.get("/git/demo/info/refs",
                         params={"service": "git-receive-pack"}, auth=AUTH)
    assert r.status_code == 403          # pull-only surface


async def test_unknown_project_is_404(client):
    r = await client.get("/git/nope/info/refs",
                         params={"service": "git-upload-pack"}, auth=AUTH)
    assert r.status_code == 404
    # a traversal-shaped slug never resolves to a path
    r = await client.get("/git/..%2f..%2fetc/info/refs",
                         params={"service": "git-upload-pack"}, auth=AUTH)
    assert r.status_code == 404


async def test_index_lists_clone_commands(client):
    r = await client.get("/git", auth=AUTH)
    assert r.status_code == 200
    assert "git clone" in r.text and "/git/demo" in r.text
    # gated too
    assert (await client.get("/git")).status_code == 401


async def test_disabled_flag_hides_everything(client, monkeypatch):
    monkeypatch.setattr(settings, "git_serve_enabled", False)
    assert (await client.get("/git", auth=AUTH)).status_code == 404
    assert (await client.get("/git/demo/info/refs",
                             params={"service": "git-upload-pack"},
                             auth=AUTH)).status_code == 404


async def test_upload_pack_negotiation_end_to_end(client):
    """A real clone: ask for refs, then POST a want for the advertised tip and
    confirm a packfile comes back (upload-pack result)."""
    adv = await client.get("/git/demo/info/refs",
                           params={"service": "git-upload-pack"}, auth=AUTH)
    # first ref line after the prelude; its 40-hex object id is HEAD's tip
    body = adv.content.split(b"0000", 1)[1]
    first = body[4:]                                   # drop the 4-hex length
    want_sha = first.split(b" ", 1)[0].decode()
    assert len(want_sha) == 40
    req = (f"0032want {want_sha}\n").encode() + b"00000009done\n"
    r = await client.post("/git/demo/git-upload-pack", content=req, auth=AUTH,
                          headers={"content-type": "application/x-git-upload-pack-request"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-git-upload-pack-result"
    assert b"PACK" in r.content or b"NAK" in r.content   # negotiation happened
