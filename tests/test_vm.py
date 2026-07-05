"""Host-side VM bridge tests. The QEMU/SSH parts need the real Pi, so these
cover what runs on the host regardless: tar packing rules, path guards, and
the API's refusal behaviors."""
import io
import tarfile

import httpx
import pytest

from backend.agent.tools import vm
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


@pytest.fixture
async def client(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_dir", tmp_env / "data" / "vm")
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
        yield c


def test_make_tar_excludes_junk(tmp_path):
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "main.py").write_text("print(1)")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: nope")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (tmp_path / ".workspace.json").write_text("{}")
    data = vm._make_tar(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
    assert "code/main.py" in names
    assert not any(".git" in n or "__pycache__" in n or ".workspace.json" in n
                   for n in names)


def test_make_tar_size_cap(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(settings, "vm_push_max_mb", 1)
    (tmp_path / "big.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    with pytest.raises(vm.VMError):
        vm._make_tar(tmp_path)


async def test_nuke_requires_confirm(client):
    r = await client.post("/api/vm/nuke", json={})
    assert r.status_code == 400


async def test_pull_rejects_escapes(client):
    r = await client.post("/api/vm/pull",
                          json={"project": "demo", "remote_path": "../etc"})
    assert r.status_code == 400
    r = await client.post("/api/vm/pull",
                          json={"project": "demo", "remote_path": "/etc"})
    assert r.status_code == 400
    r = await client.post("/api/vm/pull",
                          json={"project": "demo", "remote_path": "ok",
                                "dest": "../../outside"})
    assert r.status_code == 400


async def test_push_unknown_project(client):
    r = await client.post("/api/vm/push", json={"project": "nope"})
    assert r.status_code == 404


async def test_run_empty_command(client):
    r = await client.post("/api/vm/run", json={"command": "  "})
    assert r.status_code == 400


async def test_status_without_vm(client):
    r = await client.get("/api/vm/status")
    assert r.status_code == 200
    body = r.json()
    assert body["base_built"] is False
    assert body["ssh_ready"] is False
