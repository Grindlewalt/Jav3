"""OpenClaw skill importer: vendors a pinned, ungranted snapshot and refuses
everything a skill folder should not contain."""
import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

from backend import cli, skillimport
from backend.agent.tools import imported, registry
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.skillimport import SkillImportError, import_skill

FIXTURES = Path(__file__).parent / "fixtures" / "openclaw"


@pytest.fixture(autouse=True)
def fresh_alerts():
    imported._alerted.clear()
    yield
    imported._alerted.clear()


@pytest.fixture
def src(tmp_path):
    """A writable copy of the weather fixture to mutate."""
    d = tmp_path / "src" / "weather"
    shutil.copytree(FIXTURES / "weather", d)
    return d


def skill(tmp_path, name="thing", body="do it", extra=None) -> Path:
    d = tmp_path / "src" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n{body}\n")
    for rel, data in (extra or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(data if isinstance(data, bytes) else data.encode())
    return d


async def security_kinds():
    db = await get_db()
    try:
        async with db.execute("SELECT kind FROM security_events") as c:
            return [r["kind"] for r in await c.fetchall()]
    finally:
        await db.close()


# --- happy path ------------------------------------------------------------------

async def test_import_folder_vendors_pinned_and_ungranted(tmp_env, src):
    await init_db()
    (src / "helper.sh").write_text("echo hi\n")
    os.chmod(src / "helper.sh", 0o755)
    r = import_skill(str(src))
    dest = settings.skills_dir / "oc-weather"
    assert r["slug"] == "oc-weather" and r["name"] == "weather" and r["files"] == 2
    pin = json.loads((dest / ".pin.json").read_text())
    assert pin["origin"] == "openclaw" and pin["source"] == str(src.resolve())
    assert set(pin["files"]) == {"SKILL.md", "helper.sh"}
    assert imported.verify_pin(dest) is None
    assert (dest / "helper.sh").stat().st_mode & 0o777 == 0o644     # no exec bit
    [e] = [e for e in registry.load_registry() if e["name"] == "weather"]
    assert e["origin"] == "openclaw" and e["granted"] is False
    assert "weather" not in {s["function"]["name"] for s in registry.openai_tool_specs()}
    # weather's curl fallback is a code block -> an advisory network flag
    assert any(f["trigger"] == "network_call" for f in r["flags"])
    assert "skill_import_flag" in await security_kinds()


def test_all_four_real_fixtures_import(tmp_env):
    for n in ("weather", "github", "1password", "summarize"):
        r = import_skill(str(FIXTURES / n))
        assert r["slug"] == f"oc-{n}" and not r["blocked"]
    assert {e["name"] for e in registry.load_registry() if e.get("origin")} == {
        "weather", "github", "1password", "summarize"}


def test_install_hints_are_never_run(tmp_env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the importer ran a process for a local import")
    monkeypatch.setattr(skillimport.subprocess, "run", boom)
    r = import_skill(str(FIXTURES / "github"))
    assert r["install_hints"] == ["Install GitHub CLI (brew)"]


def test_reimport_needs_replace_and_revokes_the_grant(tmp_env, src):
    import_skill(str(src))
    dest = settings.skills_dir / "oc-weather"
    imported.set_grant(dest, True)
    with pytest.raises(SkillImportError, match="already imported"):
        import_skill(str(src))
    (src / "SKILL.md").write_text((src / "SKILL.md").read_text() + "\nv2\n")
    import_skill(str(src), replace=True)
    assert imported.verify_pin(dest) is None
    assert not imported.granted(dest, imported.read_pin(dest))


# --- refusals ----------------------------------------------------------------------

def test_name_clash_with_a_tool_is_refused_and_name_overrides(tmp_env, tmp_path):
    d = skill(tmp_path, "web_search")
    with pytest.raises(SkillImportError, match="taken by the tool"):
        import_skill(str(d))
    r = import_skill(str(d), name="oc_web_search_alt")
    assert r["name"] == "oc_web_search_alt"
    names = {e["name"] for e in registry.load_registry() if e.get("origin")}
    assert names == {"oc_web_search_alt"}


@pytest.mark.parametrize("extra,match", [
    ({"bin/payload": b"\x7fELF\x00\x01\xff\xfe"}, "not UTF-8"),
    ({"big.md": "a" * (300 * 1024)}, "larger than"),
    ({f"r/{i}.md": "x" for i in range(70)}, "more than"),
])
def test_bad_bundles_are_refused(tmp_env, tmp_path, extra, match):
    with pytest.raises(SkillImportError, match=match):
        import_skill(str(skill(tmp_path, extra=extra)))
    assert not (settings.skills_dir / "oc-thing").exists()


def test_symlink_is_refused(tmp_env, tmp_path):
    d = skill(tmp_path)
    (d / "creds").symlink_to(Path.home())
    with pytest.raises(SkillImportError, match="symlink"):
        import_skill(str(d))


def test_missing_skill_md_and_frontmatter(tmp_env, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SkillImportError, match="no SKILL.md"):
        import_skill(str(empty))
    d = tmp_path / "nofm"
    d.mkdir()
    (d / "SKILL.md").write_text("just text\n")
    with pytest.raises(SkillImportError, match="frontmatter"):
        import_skill(str(d))


def test_invalid_name_is_refused(tmp_env, tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: 'a b; rm'\ndescription: x\n---\n")
    with pytest.raises(SkillImportError, match="not a valid tool name"):
        import_skill(str(d))


# --- git -----------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["git@github.com:o/r.git", "ssh://h/r.git",
                                 "file:///etc", "ext::sh -c id", "http://example.com/r.git"])
def test_non_https_git_urls_are_refused(tmp_env, url):
    with pytest.raises(SkillImportError):
        import_skill(url)


def test_private_git_host_is_refused_unless_allowlisted(tmp_env, monkeypatch):
    calls = []

    def fake_clone(url, subdir, dest):
        calls.append((url, subdir))
        shutil.copytree(FIXTURES / "weather", dest / subdir)
        return "abc123"
    monkeypatch.setattr(skillimport, "_clone", fake_clone)
    with pytest.raises(SkillImportError, match="non-public"):
        import_skill("https://127.0.0.1/skills.git#weather")
    assert calls == []
    monkeypatch.setattr(settings, "skill_import_allow_hosts", "127.0.0.1")
    r = import_skill("http://127.0.0.1/skills.git#weather")
    assert calls == [("http://127.0.0.1/skills.git", "weather")]
    assert r["ref"] == "abc123" and r["source"] == "http://127.0.0.1/skills.git#weather"


def test_git_subdir_traversal_is_refused(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "skill_import_allow_hosts", "127.0.0.1")
    with pytest.raises(SkillImportError, match="bad sub-directory"):
        import_skill("https://127.0.0.1/r.git#../../etc")


# --- ClawHub -------------------------------------------------------------------------

def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return buf.getvalue()


def _hub(monkeypatch, info: dict, download: bytes):
    def handler(req: httpx.Request):
        if req.url.path.endswith("/download"):
            return httpx.Response(200, content=download,
                                  headers={"content-type": "application/zip"})
        return httpx.Response(200, json=info)
    monkeypatch.setattr(settings, "skill_import_clawhub", True)
    monkeypatch.setattr(skillimport, "_safe_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_clawhub_is_off_by_default(tmp_env):
    with pytest.raises(SkillImportError, match="ClawHub import is off"):
        import_skill("clawhub:weather")


def test_clawhub_zip_import(tmp_env, monkeypatch):
    md = (FIXTURES / "weather" / "SKILL.md").read_bytes()
    _hub(monkeypatch, {"skill": {"slug": "weather"}, "latestVersion": {"version": "1.2.3"}},
         _zip({"weather-1.2.3/SKILL.md": md}))
    r = import_skill("clawhub:weather")
    assert r["slug"] == "oc-weather" and r["ref"] == "1.2.3"
    assert r["source"] == "clawhub:weather"


def test_clawhub_flagged_skill_is_refused(tmp_env, monkeypatch):
    _hub(monkeypatch, {"moderation": {"isSuspicious": True, "verdict": "suspicious"}},
         b"")
    with pytest.raises(SkillImportError, match="suspicious"):
        import_skill("clawhub:evil")


def test_clawhub_zip_slip_is_refused(tmp_env, monkeypatch):
    _hub(monkeypatch, {}, _zip({"SKILL.md": b"---\nname: a\ndescription: b\n---\n",
                               "../../escape.txt": b"x"}))
    with pytest.raises(SkillImportError, match="unsafe path"):
        import_skill("clawhub:slip")


# --- CLI + API -----------------------------------------------------------------------

def test_cli_import_skill(tmp_env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cli", "import-skill", str(FIXTURES / "github"),
                                      "--name", "gh_cli"])
    cli.main()
    out = capsys.readouterr().out
    assert "imported gh_cli -> skills/oc-gh-cli" in out and "not granted" in out
    assert "NEED  program     gh" in out and "(not run)" in out


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
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        yield c


async def test_api_import(client, src):
    r = await client.post("/api/skills/import", json={"source": str(src)})
    assert r.status_code == 200 and r.json()["slug"] == "oc-weather"
    r = await client.post("/api/skills/import", json={"source": str(src)})
    assert r.status_code == 400 and "already imported" in r.json()["detail"]
    tools = {t["name"]: t for t in (await client.get("/api/tools")).json()["tools"]}
    assert tools["weather"]["group"] == "imported"


async def test_api_import_needs_the_operator_cookie(tmp_env, src):
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/skills/import", json={"source": str(src)})
    assert r.status_code == 401
    assert not (settings.skills_dir / "oc-weather").exists()
