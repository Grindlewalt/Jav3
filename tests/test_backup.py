"""rclone backups with rclone itself mocked: the argv, the DB snapshot, the
crypt-only secrets rule, and a status that survives rclone being absent."""
import sqlite3
import subprocess
from pathlib import Path

import pytest

from backend import backup
from backend.config import settings

from test_api import client  # noqa: F401  (fixture)


class FakeRclone:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        call = {"argv": list(argv), "env": kw.get("env") or {},
                "input": kw.get("input")}
        if argv[1] == "copyto" and argv[-1].endswith("data/jarvis.db") \
                and not argv[-2].startswith(("r:", "jav3crypt:")):
            snap = Path(argv[-2])
            con = sqlite3.connect(snap)
            call["snapshot_rows"] = con.execute(
                "SELECT COUNT(*) FROM t").fetchone()[0]
            con.close()
        if argv[1] == "sync" and argv[-1] == "jav3crypt:":
            call["staged"] = sorted(p.name for p in Path(argv[-2]).iterdir())
        self.calls.append(call)
        out = "OBSCURED\n" if argv[1] == "obscure" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


@pytest.fixture
def rc(tmp_env, monkeypatch):
    fake = FakeRclone()
    monkeypatch.setattr(backup.subprocess, "run", fake)
    monkeypatch.setattr(backup.shutil, "which", lambda name: "/usr/bin/rclone")
    for d in ("memory", "projects", "agents", "skills"):
        (tmp_env / d).mkdir(parents=True, exist_ok=True)
        (tmp_env / d / "f.md").write_text(d)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(settings.db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (x)")
    con.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
    con.commit()
    con.close()
    cfg = backup.load_config()
    cfg["remote"] = "r:bk"
    backup.save_config(cfg)
    return fake


def _argvs(fake):
    return [c["argv"] for c in fake.calls]


def test_backup_argv_and_snapshot(rc, tmp_env):
    st = backup.run_backup()
    assert st["ok"] and st["bytes"] > 0
    argvs = _argvs(rc)
    for name in ("memory", "projects", "agents", "skills"):
        sync = next(a for a in argvs if a[1] == "sync" and a[3] == f"r:bk/{name}")
        assert sync[0] == "/usr/bin/rclone"
        assert sync[2] == str(tmp_env / name)
        assert "--exclude" in sync and "__pycache__/**" in sync
    copy = next(c for c in rc.calls if c["argv"][1] == "copyto")
    assert copy["argv"][-1] == "r:bk/data/jarvis.db"
    assert "--checksum" in copy["argv"]
    assert copy["snapshot_rows"] == 2          # a real, consistent snapshot
    assert not Path(copy["argv"][-2]).exists()  # temp snapshot cleaned up
    # no secrets anywhere by default
    assert not any("secrets" in " ".join(a) for a in argvs)
    assert backup.status()["last"]["ok"] is True


def test_remote_root_join(rc):
    cfg = backup.load_config()
    cfg["remote"] = "r:"
    backup.save_config(cfg)
    backup.run_backup()
    assert any(a[1] == "sync" and "r:memory" in a for a in _argvs(rc))


def test_secrets_refused_without_crypt(rc):
    cfg = backup.load_config()
    cfg["include_secrets"] = True
    backup.save_config(cfg)
    with pytest.raises(backup.BackupError, match="plaintext"):
        backup.run_backup()
    assert rc.calls == []                       # nothing uploaded at all


def test_secrets_go_only_through_crypt(rc, tmp_env):
    settings.secrets_path.write_text('{"K": "v"}')
    (settings.data_dir / "jwt_secret").write_text("jwt")
    cfg = backup.load_config()
    cfg.update(include_secrets=True, crypt_password="hunter2",
               crypt_password2="salt")
    backup.save_config(cfg)
    backup.run_backup()
    sec = next(c for c in rc.calls if c["argv"][1] == "sync"
               and c["argv"][-1] == "jav3crypt:")
    assert sec["env"]["RCLONE_CONFIG_JAV3CRYPT_TYPE"] == "crypt"
    assert sec["env"]["RCLONE_CONFIG_JAV3CRYPT_REMOTE"] == "r:bk/secrets"
    assert sec["env"]["RCLONE_CONFIG_JAV3CRYPT_PASSWORD"] == "OBSCURED"
    assert sec["env"]["RCLONE_CONFIG_JAV3CRYPT_PASSWORD2"] == "OBSCURED"
    assert {"secrets.json", "jwt_secret"} <= set(sec["staged"])
    # the password rode stdin to `rclone obscure -`, never an argv
    obscure = [c for c in rc.calls if c["argv"][1] == "obscure"]
    assert obscure and obscure[0]["argv"][-1] == "-"
    assert {c["input"] for c in obscure} == {"hunter2", "salt"}
    assert not any("hunter2" in " ".join(c["argv"]) for c in rc.calls)
    # and never a plaintext path to the remote
    assert not any(a[-1].startswith("r:bk/secrets") for a in _argvs(rc))


def test_user_crypt_remote(rc):
    settings.secrets_path.write_text("{}")
    cfg = backup.load_config()
    cfg.update(include_secrets=True, crypt_remote="mycrypt:")
    backup.save_config(cfg)
    backup.run_backup()
    assert any(a[1] == "sync" and a[-1] == "mycrypt:" for a in _argvs(rc))
    assert not any(a[1] == "obscure" for a in _argvs(rc))


def test_rclone_absent_status_says_so(tmp_env, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda name: None)
    st = backup.status()
    assert st["rclone"]["available"] is False
    cfg = backup.load_config()
    cfg["remote"] = "r:bk"
    backup.save_config(cfg)
    with pytest.raises(backup.BackupError, match="not installed"):
        backup.run_backup()


def test_rclone_failure_recorded(rc, monkeypatch):
    def boom(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="",
                                           stderr="ERROR : quota exceeded")
    monkeypatch.setattr(backup.subprocess, "run", boom)
    with pytest.raises(backup.BackupError, match="quota"):
        backup.run_backup()
    last = backup.status()["last"]
    assert last["ok"] is False and "quota" in last["error"]


def test_restore_mirrors(rc, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "service_busy", lambda p: None)

    def fake(argv, **kw):
        rc.calls.append({"argv": list(argv), "env": kw.get("env") or {}})
        if argv[1] == "copyto":                 # "download" a valid DB
            con = sqlite3.connect(argv[-1])
            con.execute("CREATE TABLE t (x)")
            con.commit()
            con.close()
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(backup.subprocess, "run", fake)
    to = tmp_path / "restored"
    lines = backup.restore("r:bk", to)
    argvs = _argvs(rc)
    assert ["/usr/bin/rclone", "copy", "r:bk/memory", str(to / "memory")] in argvs
    assert (to / "data" / "jarvis.db").exists()
    assert any("integrity ok" in line for line in lines)
    # refuses to land on existing state without force
    with pytest.raises(backup.BackupError, match="already holds"):
        backup.restore("r:bk", to)


async def test_backup_api(client, tmp_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(backup.shutil, "which", lambda name: None)
    r = await client.get("/api/backup/status")
    assert r.status_code == 401
    await client.post("/api/auth/login",
                      json={"username": "operator", "password": "hunter2"})
    r = await client.get("/api/backup/status")
    assert r.status_code == 200 and r.json()["rclone"]["available"] is False

    r = await client.put("/api/backup/config", json={"remote": "--config=/x"})
    assert r.status_code == 400
    r = await client.put("/api/backup/config", json={"include_secrets": True})
    assert r.status_code == 400                 # no crypt configured
    r = await client.put("/api/backup/config",
                         json={"remote": "r:bk", "include_secrets": True,
                               "crypt_password": "pw"})
    body = r.json()
    assert r.status_code == 200 and body["crypt_password_set"] is True
    assert "pw" not in r.text
    r = await client.get("/api/backup/config")
    assert r.json()["remote"] == "r:bk" and "crypt_password" not in r.json()
    assert (settings.secrets_path.parent / "backup.json").stat().st_mode & 0o077 == 0

    r = await client.post("/api/backup/run")
    assert r.status_code == 400 and "not installed" in r.json()["detail"]
