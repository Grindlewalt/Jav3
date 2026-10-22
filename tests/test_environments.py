"""Environment requests (docs/agent-requests-contract.md, 1): parsing, filing,
approval/rejection, the operator API and the tool handler. Offline."""
import httpx
import pytest

from backend import environments, packages, profiles
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.vm import images

PE = packages.PackageError


# --- parse_packages ------------------------------------------------------------

def test_parse_objects_and_strings():
    out = environments.parse_packages([
        {"manager": "apt", "package": "mesa-utils"},
        {"manager": "pip", "package": "Pygame", "version": "2.6.1"},
        "apt ffmpeg", "apt:git", "pip numpy==1.26.4", "npm typescript@5.6.3",
        "npm @types/node@22.0.0", "npm  eslint"])
    got = [(p["manager"], p["package"], p["version"]) for p in out]
    assert got == [("apt", "mesa-utils", None), ("pip", "pygame", "2.6.1"),
                   ("apt", "ffmpeg", None), ("apt", "git", None),
                   ("pip", "numpy", "1.26.4"), ("npm", "typescript", "5.6.3"),
                   ("npm", "@types/node", "22.0.0"), ("npm", "eslint", None)]


def test_parse_collapses_duplicates():
    out = environments.parse_packages(["apt ffmpeg", "apt:ffmpeg",
                                       {"manager": "apt", "package": "ffmpeg"}])
    assert len(out) == 1


@pytest.mark.parametrize("bad", [
    "apt x;id", "pip git+https://evil/x", "npm https://x/y.tgz", "apt --index-url=x",
    "pip foo>=1", "brew wget", "ffmpeg", "", "apt", {"manager": "apt"},
    {"manager": "pip", "package": "ok", "version": ">=1"}, 5, None,
    {"manager": "npm", "package": "x", "version": "1.0"},
])
def test_parse_refuses_bad(bad):
    with pytest.raises(PE):
        environments.parse_packages([bad])


def test_parse_empty_and_too_many():
    with pytest.raises(PE):
        environments.parse_packages([])
    with pytest.raises(PE):
        environments.parse_packages(None)
    many = [f"apt pkg{i:03d}" for i in range(environments.MAX_PACKAGES + 1)]
    with pytest.raises(PE, match="at most"):
        environments.parse_packages(many)
    assert len(environments.parse_packages(many[:-1])) == environments.MAX_PACKAGES


# --- db ------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_env, monkeypatch):
    monkeypatch.setattr(images.builder, "kick_resolve", lambda: None)
    await init_db()
    d = await get_db()
    await d.execute(
        "INSERT INTO security_profiles(name, builtin, is_default, box_image, "
        "allow_package_requests, service_placement, box_runtime) "
        "VALUES ('Default', 1, 1, 'main', 1, 'per_project', 'kvm')")
    await d.execute(
        "INSERT INTO security_profiles(name, builtin, box_image, allow_package_requests, "
        "service_placement, box_runtime) VALUES ('Locked', 0, 'main', 0, 'per_project', 'kvm')")
    for i, (slug, prof) in enumerate([("a", None), ("b", None), ("d", "Locked")]):
        await d.execute(
            "INSERT INTO projects(id, slug, name, path, profile_id) VALUES "
            "(?, ?, ?, ?, (SELECT id FROM security_profiles WHERE name = ?))",
            (i + 1, slug, slug, f"/p/{slug}", prof))
    await d.commit()
    yield d
    await d.close()


async def _events(db, kind):
    async with db.execute("SELECT * FROM security_events WHERE kind = ?", (kind,)) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _file(db, name="sim-gl", project="a", **kw):
    args = dict(project=project, conversation_id=5, name=name, from_variant="main",
                packages=["apt mesa-utils", "pip pygame==2.6.1"], min_mem_mb=1536,
                reason="WebGL needs software GL")
    args.update(kw)
    return await environments.file_request(db, **args)


async def _resolve(db, env):
    for p in env["packages"]:
        await packages.set_resolution(db, p["id"], resolved_version={
            "apt": "9.0.0-2", "pip": "2.6.1", "npm": "1.0.0"}[p["manager"]],
            integrity="sha256:ab")


async def _profile_of(db, slug):
    return await profiles.for_slug(db, slug)


# --- file_request ---------------------------------------------------------------

async def test_file_creates_rows_and_one_event(db):
    env = await _file(db)
    assert env["status"] == "pending" and env["name"] == "sim-gl"
    assert [p["package"] for p in env["packages"]] == ["mesa-utils", "pygame"]
    assert env["resolved"] is False
    assert env["profile"] is None and env["build"] is None
    assert "builds image `sim-gl` from `main`" in env["plan"]
    async with db.execute("SELECT environment_id, target_variant, status "
                          "FROM package_catalogue") as cur:
        rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [(env["id"], "sim-gl", "pending")] * 2
    assert len(await _events(db, "environment_requested")) == 1
    assert await _events(db, "package_requested") == []
    assert await packages.list_rows(db) == []
    assert len(await packages.list_rows(db, include_environment=True)) == 2


async def test_file_refusals(db):
    with pytest.raises(PE, match="active project"):
        await _file(db, project=None)
    with pytest.raises(PE, match="does not allow"):
        await _file(db, project="d")
    with pytest.raises(PE):
        await _file(db, name="Bad Name!")
    with pytest.raises(PE, match="builtin"):
        await _file(db, name="main")
    await images.create_variant(db, "taken", "main", [])
    with pytest.raises(PE, match="exists"):
        await _file(db, name="taken")
    with pytest.raises(PE, match="no image variant"):
        await _file(db, from_variant="nope")
    for mem in (100, 99999, "big"):
        with pytest.raises(PE, match="min_mem_mb"):
            await _file(db, min_mem_mb=mem)
    with pytest.raises(PE, match="reason"):
        await _file(db, reason=" ")
    with pytest.raises(PE, match="at most"):
        await _file(db, packages=[f"apt p{i:03d}" for i in range(31)])
    # nothing half-filed by the refusals
    assert await environments.list_rows(db) == []


async def test_file_duplicate_and_pending_cap(db):
    await _file(db, name="one")
    with pytest.raises(PE, match="already the name"):
        await _file(db, name="one", project="b")
    await _file(db, name="two")
    await _file(db, name="three")
    with pytest.raises(PE, match="3 pending"):
        await _file(db, name="four")
    await _file(db, name="four", project="b")          # per project


async def test_bad_package_files_nothing(db):
    with pytest.raises(PE):
        await _file(db, packages=["apt ok1", "pip git+https://x/y"])
    assert await environments.list_rows(db) == []


# --- approve / reject -----------------------------------------------------------

async def test_approve_refused_until_resolved(db):
    env = await _file(db)
    with pytest.raises(PE, match="not resolved"):
        await environments.approve(db, env["id"], decided_by="op", by_operator=True)
    await packages.set_resolution(db, env["packages"][0]["id"],
                                  resolved_version="9.0.0-2", integrity="sha256:ab")
    with pytest.raises(PE, match="not resolved"):
        await environments.approve(db, env["id"], decided_by="op", by_operator=True)
    assert not await images.variant_exists(db, "sim-gl")


async def test_approve_builds_variant_and_moves_only_this_project(db):
    env = await _file(db)
    await _resolve(db, env)
    out = await environments.approve(db, env["id"], decided_by="op", by_operator=True)
    assert out["status"] == "approved" and out["decided_by"] == "op"
    assert out["profile"]["name"] == "a · sim-gl"
    info = (await images.sync_variants(db))["sim-gl"]
    assert info["from"] == "main" and info["min_mem_mb"] == 1536
    assert {p["package"] for p in info["effective"]} >= {"mesa-utils", "pygame"}
    async with db.execute("SELECT status, target_variant FROM package_catalogue "
                          "WHERE environment_id = ?", (env["id"],)) as cur:
        assert {tuple(r) for r in await cur.fetchall()} == {("approved", "sim-gl")}
    pa, pb = await _profile_of(db, "a"), await _profile_of(db, "b")
    assert pa["box_image"] == "sim-gl" and not pa["is_default"]
    assert pb["box_image"] == "main" and pb["name"] == "Default"
    assert (await profiles.current_default(db))["box_image"] == "main"
    async with db.execute("SELECT profile_id, profile_copied_from FROM "
                          "environment_requests WHERE id = ?", (env["id"],)) as cur:
        r = await cur.fetchone()
    assert r[0] == pa["id"] and r[1] == (await profiles.current_default(db))["id"]
    ev = await _events(db, "environment_approved")
    assert len(ev) == 1 and ev[0]["severity"] == "warn"
    assert (await environments.get(db, env["id"]))["build"] is None


async def test_second_environment_updates_own_profile(db):
    e1 = await _file(db, name="first")
    await _resolve(db, e1)
    await environments.approve(db, e1["id"], decided_by="op", by_operator=True)
    pid = (await _profile_of(db, "a"))["id"]
    e2 = await _file(db, name="second", packages=["apt git"])
    assert "its own profile" in e2["plan"]
    await _resolve(db, e2)
    out = await environments.approve(db, e2["id"], decided_by="op", by_operator=True)
    pa = await _profile_of(db, "a")
    assert pa["id"] == pid and pa["box_image"] == "second"
    assert out["profile"]["id"] == pid
    async with db.execute("SELECT profile_copied_from FROM environment_requests "
                          "WHERE id = ?", (e2["id"],)) as cur:
        assert (await cur.fetchone())[0] is None
    async with db.execute("SELECT COUNT(*) FROM security_profiles") as cur:
        assert (await cur.fetchone())[0] == 3             # Default, Locked, a · first


async def test_reject_rejects_packages(db):
    env = await _file(db)
    out = await environments.reject(db, env["id"], reason="no", decided_by="op",
                                    by_operator=True)
    assert out["status"] == "rejected"
    assert {p["status"] for p in out["packages"]} == {"rejected"}
    assert len(await _events(db, "environment_rejected")) == 1
    assert await _events(db, "package_rejected") == []
    with pytest.raises(PE, match="already rejected"):
        await environments.reject(db, env["id"], decided_by="op", by_operator=True)
    with pytest.raises(PE, match="already rejected"):
        await environments.approve(db, env["id"], decided_by="op", by_operator=True)
    with pytest.raises(LookupError):
        await environments.approve(db, 999, decided_by="op", by_operator=True)


async def test_package_row_alone_refused_while_pending(db):
    env = await _file(db)
    pid = env["packages"][0]["id"]
    await _resolve(db, env)
    with pytest.raises(PE, match=f"environment request #{env['id']}"):
        await packages.approve(db, pid, target_variant=None)
    with pytest.raises(PE, match="decide it there"):
        await packages.reject(db, pid)


# --- API ------------------------------------------------------------------------

@pytest.fixture
async def client(db):
    from backend.main import app
    await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                     ("operator", hash_password("hunter2")))
    await db.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _login(c):
    r = await c.post("/api/auth/login", json={"username": "operator",
                                              "password": "hunter2"})
    assert r.status_code == 200


async def test_api_requires_login(client):
    assert (await client.get("/api/environments")).status_code == 401
    r = await client.post("/api/environments/1/approve", json={"acknowledge": True})
    assert r.status_code == 401
    r = await client.post("/api/environments/1/approve", json={"acknowledge": True},
                          headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_api_flow(client, db, monkeypatch):
    env = await _file(db)
    await _login(client)
    r = await client.get("/api/environments?status=pending&project=a")
    assert r.status_code == 200 and r.json()["environments"][0]["id"] == env["id"]
    r = await client.post(f"/api/environments/{env['id']}/approve", json={})
    assert r.status_code == 400
    r = await client.post("/api/environments/999/approve", json={"acknowledge": True})
    assert r.status_code == 404
    r = await client.post(f"/api/environments/{env['id']}/approve",
                          json={"acknowledge": True})
    assert r.status_code == 409                          # not resolved
    await _resolve(db, env)
    started = []
    from backend import environments_api
    monkeypatch.setattr(environments_api, "_start_build", lambda v: started.append(v))
    monkeypatch.setattr(settings, "vm_boxes_enabled", False)
    r = await client.post(f"/api/environments/{env['id']}/approve",
                          json={"acknowledge": True})
    assert r.status_code == 200 and r.json()["build_started"] is False
    assert r.json()["status"] == "approved" and started == []
    r = await client.post(f"/api/environments/{env['id']}/approve",
                          json={"acknowledge": True})
    assert r.status_code == 409                          # wrong state
    r = await client.post(f"/api/environments/{env['id']}/reject", json={"reason": "x"})
    assert r.status_code == 409
    r = await client.post("/api/environments/999/reject", json={})
    assert r.status_code == 404


async def test_api_build_starts_when_boxes_on(client, db, monkeypatch):
    env = await _file(db)
    await _resolve(db, env)
    await _login(client)
    started = []
    from backend import environments_api
    monkeypatch.setattr(environments_api, "_start_build", lambda v: started.append(v))
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    r = await client.post(f"/api/environments/{env['id']}/approve",
                          json={"acknowledge": True})
    assert r.status_code == 200 and r.json()["build_started"] is True
    assert started == ["sim-gl"]


async def test_api_reject(client, db):
    env = await _file(db)
    await _login(client)
    r = await client.post(f"/api/environments/{env['id']}/reject", json={"reason": "no"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"


# --- tool handler ---------------------------------------------------------------

def _tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "t_envreq", settings.base_dir / "tools" / "environment_request" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_tool_handler(db, monkeypatch):
    mod = _tool()
    monkeypatch.setattr(settings, "vm_boxes_enabled", False)
    out = await mod.run(name="sim-gl", packages=["apt ffmpeg"], reason="r")
    assert out.startswith("error:") and "vm_boxes_enabled" in out
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    from backend import runtime
    tok = runtime.active_project.set("a")
    try:
        out = await mod.run(name="sim-gl", packages=["pip git+https://x/y"], reason="r")
        assert out.startswith("error:")
        out = await mod.run(name="sim-gl", packages=["apt mesa-utils", "pip pygame"],
                            reason="WebGL", **{"from": "main"})
    finally:
        runtime.active_project.reset(tok)
    assert out == ("environment request #1 filed: image `sim-gl` from `main` with 2 "
                   "packages — pending. Nothing is built until the operator approves "
                   "it; this project's boxes then boot on it (a later box boot, not "
                   "this turn).")


async def _open(db, kind):
    async with db.execute("SELECT COUNT(*) FROM security_events WHERE kind = ? "
                          "AND acknowledged = 0", (kind,)) as cur:
        return (await cur.fetchone())[0]


async def test_deciding_settles_only_that_request(db):
    one = await _file(db, name="one")
    two = await _file(db, name="two")
    assert await _open(db, "environment_requested") == 2
    await _resolve(db, one)
    await environments.approve(db, one["id"], decided_by="op", by_operator=True)
    assert await _open(db, "environment_requested") == 1
    await environments.reject(db, two["id"], decided_by="op", by_operator=True)
    assert await _open(db, "environment_requested") == 0
