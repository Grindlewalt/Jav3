"""M6: archive upload, codebase indexing, code search."""
import io
import zipfile

import httpx
import pytest

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


def make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def upload(client, data: bytes, name="repo.zip", dest=None):
    form = {"dest": dest} if dest is not None else {}
    return await client.post("/api/projects/demo/upload_archive",
                             files={"file": (name, data, "application/zip")},
                             data=form)


async def test_upload_archive_strips_single_top_dir(client):
    z = make_zip({
        "myrepo-main/README.md": b"# repo\n",
        "myrepo-main/src/app.py": b"def main():\n    pass\n",
        "myrepo-main/src/utils/helpers.py": b"class Helper:\n    pass\n",
        "myrepo-main/.git/config": b"[core]\n",   # skipped
    })
    r = await upload(client, z)
    assert r.status_code == 200
    body = r.json()
    assert body["files"] == 3 and body["dest"] == "code"
    base = settings.projects_dir / "demo" / "code"
    assert (base / "README.md").exists()
    assert (base / "src/utils/helpers.py").exists()
    assert not (base / ".git").exists()
    assert body["bytes"] == sum(len(b) for n, b in [
        ("a", b"# repo\n"), ("b", b"def main():\n    pass\n"),
        ("c", b"class Helper:\n    pass\n")])


async def test_upload_archive_zip_slip_skipped(client):
    import stat as statmod
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", b"pwn")
        zf.writestr("/abs/evil.txt", b"pwn")
        link = zipfile.ZipInfo("link.txt")
        link.external_attr = (statmod.S_IFLNK | 0o777) << 16
        zf.writestr(link, b"/etc/passwd")
        zf.writestr("ok.txt", b"fine")
    r = await upload(client, buf.getvalue())
    assert r.status_code == 200
    assert r.json()["files"] == 1
    proj = settings.projects_dir / "demo"
    assert (proj / "code/ok.txt").exists()
    assert not list(proj.parent.parent.rglob("evil.txt"))


async def test_upload_archive_size_cap_413_no_partials(client, monkeypatch):
    monkeypatch.setattr(settings, "upload_max_uncompressed_mb", 1)
    z = make_zip({"small.txt": b"ok", "big.bin": b"a" * (2 * 1024 * 1024)})
    r = await upload(client, z)
    assert r.status_code == 413
    code = settings.projects_dir / "demo" / "code"
    assert not code.exists() or not list(code.rglob("*"))


async def test_upload_archive_file_count_cap(client, monkeypatch):
    monkeypatch.setattr(settings, "upload_max_files", 2)
    z = make_zip({f"f{i}.txt": b"x" for i in range(3)})
    r = await upload(client, z)
    assert r.status_code == 413


async def test_upload_archive_rejects_non_zip(client):
    r = await upload(client, b"just some text", name="notes.txt")
    assert r.status_code == 400
    r = await upload(client, b"not a zip at all", name="fake.zip")
    assert r.status_code == 400


def _seed_code():
    code = settings.projects_dir / "demo" / "code"
    (code / "src").mkdir(parents=True, exist_ok=True)
    (code / "src" / "app.py").write_text(
        "class Engine:\n    pass\n\ndef ignite(fuel):\n    return 'needle_xyz'\n")
    (code / "main.go").write_text("func Run() {}\ntype Config struct {}\n")
    (code / "blob.bin").write_bytes(b"\x00" * 64)
    (code / "huge.txt").write_text("x" * (600 * 1024))
    return code


async def test_crawl_codebase_builds_index(client):
    _seed_code()
    out = await registry.dispatch("crawl_codebase", {})
    assert "indexed 2 files" in out and "search_codebase" in out
    notes = settings.projects_dir / "demo" / "notes" / "codebase"
    index = (notes / "INDEX.md").read_text()
    assert "src/app.py" in index and "main.go" in index
    assert "class Engine" in index
    assert "blob.bin" not in index and "huge.txt" not in index
    detail = (notes / "src.md").read_text()
    assert "class Engine" in detail and "def ignite" in detail
    root_detail = (notes / "_root.md").read_text()
    assert "func Run" in root_detail and "type Config" in root_detail


async def test_search_codebase(client):
    _seed_code()
    out = await registry.dispatch("search_codebase", {"query": "NEEDLE_xyz"})
    assert "code/src/app.py:5:" in out and "needle_xyz" in out

    out = await registry.dispatch("search_codebase",
                                  {"query": r"def\s+ig\w+", "regex": True})
    assert "code/src/app.py:4:" in out

    out = await registry.dispatch("search_codebase", {"query": "not_here_at_all"})
    assert out == "no matches"

    out = await registry.dispatch("search_codebase",
                                  {"query": "[bad", "regex": True})
    assert out.startswith("error:")


async def test_search_codebase_cap(client):
    code = settings.projects_dir / "demo" / "code"
    code.mkdir(parents=True, exist_ok=True)
    (code / "many.txt").write_text("hit_me\n" * 80)
    out = await registry.dispatch("search_codebase", {"query": "hit_me"})
    assert "matches (truncated" in out
    assert out.count("hit_me") == 50
