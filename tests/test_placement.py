"""Per-project placement ("Runs in", backend/vm/placement.py): resolution
order (project setting > profile default), own boxes (separate from the same
image), caps, join (operator only, security events, per-project attribution,
one project's turns at a time) and the API."""
import httpx
import pytest

from backend import db as db_mod
from backend import egress, profiles, reviewer, security
from backend.config import settings
from backend.vm import boxes, egress_proxy, placement


@pytest.fixture(autouse=True)
def clean(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 10**6)
    monkeypatch.setattr(settings, "vm_max_boxes", 8)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 4)
    boxes.registry.reset()
    egress._stack.clear()
    egress._context.update(egress._EMPTY)
    yield
    boxes.registry.reset()
    egress._stack.clear()
    egress._context.update(egress._EMPTY)


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


async def add_project(db, slug, profile_id=None):
    await db.execute("INSERT INTO projects(slug, name, path, profile_id) VALUES (?,?,?,?)",
                     (slug, slug, f"/tmp/{slug}", profile_id))
    await db.commit()


async def iso_profile(db, **kw):
    body = {"name": "Iso", "service_placement": "per_project", "box_runtime": "kvm",
            "separate_box": True, "box_image": "main", "box_mem_mb": 512, **kw}
    return (await profiles.create(db, body))["id"]


async def events(db, kind):
    async with db.execute("SELECT * FROM security_events WHERE kind = ? ORDER BY id",
                          (kind,)) as cur:
        return [security._row(r) for r in await cur.fetchall()]


class _Ctl:
    def __init__(self, running=False):
        self._r, self.pid, self.booted_at, self.inflight = running, None, None, 0
        self.idle_since = 0.0

    def running(self):
        return self._r


# --- resolution (pure) ----------------------------------------------------------

def test_resolve_profile_default_and_overrides():
    shared_prof = {"separate_box": 0}
    iso = {"separate_box": 1, "box_runtime": "docker", "box_image": "dev", "box_mem_mb": 900}
    r = placement.resolve(None, shared_prof, "a")
    assert (r["mode"], r["source"], r["box_id"]) == ("shared", "profile", "shared")
    r = placement.resolve(None, iso, "a")
    assert (r["mode"], r["source"], r["box_id"]) == ("own", "profile", "p-a")
    assert (r["runtime"], r["image"], r["mem_mb"]) == ("docker", "dev", 900)
    # the project's setting wins over the profile, both ways
    r = placement.resolve({"mode": "shared"}, iso, "a")
    assert (r["mode"], r["source"]) == ("shared", "project")
    r = placement.resolve({"mode": "own", "runtime": "kvm", "image": None, "mem_mb": None},
                          iso, "a")
    assert (r["mode"], r["runtime"], r["image"], r["mem_mb"]) == ("own", "kvm", "dev", 900)
    r = placement.resolve({"mode": "own"}, shared_prof, "a")
    assert (r["runtime"], r["image"]) == ("kvm", "main")
    r = placement.resolve({"mode": "join", "box_id": "p-b"}, iso, "a")
    assert (r["mode"], r["box_id"], r["owner"]) == ("join", "p-b", "b")


def test_normalize_rules():
    n = placement.normalize
    assert n({"mode": "join:shared"}, "a")["mode"] == "shared"
    assert n({"mode": "join", "box_id": "p-a"}, "a")["mode"] == "own"     # its own box
    assert n({"mode": "join:p-b"}, "a") == {"mode": "join", "box_id": "p-b",
                                            "runtime": None, "image": None, "mem_mb": None}
    for bad in ({"mode": "join:s-b"}, {"mode": "join:b-main-9"}, {"mode": "nope"},
                {"mode": "own", "runtime": "lxc"}, {"mode": "own", "mem_mb": 10},
                {"mode": "own", "image": "Bad Name"}):
        with pytest.raises(placement.PlacementError):
            n(bad, "a")
    # shared/profile carry nothing else
    assert n({"mode": "shared", "runtime": "docker", "mem_mb": 900}, "a")["runtime"] is None


# --- allocation ------------------------------------------------------------------

async def test_project_setting_beats_profile_default(db):
    pid = await iso_profile(db)
    await add_project(db, "iso", pid)
    await add_project(db, "plain")
    assert (await boxes.for_project("iso")).id == "p-iso"
    assert (await boxes.for_project("plain")).id == "shared"
    # iso chooses the shared box explicitly: even its existing box is left
    await placement.put(db, "iso", {"mode": "shared"})
    assert (await boxes.for_project("iso")).id == "shared"
    # plain chooses its own box: a docker-free kvm box with its own memory
    await placement.put(db, "plain", {"mode": "own", "mem_mb": 640})
    b = await boxes.for_project("plain")
    assert (b.id, b.runtime, b.mem_mb) == ("p-plain", "kvm", 640)
    # back to the profile: shared again
    await placement.put(db, "plain", {"mode": "profile"})
    boxes.registry.release("p-plain")
    assert (await boxes.for_project("plain")).id == "shared"


async def test_profile_default_shared_still_uses_a_warmed_box(db):
    await add_project(db, "plain")
    boxes.allocate("project", project="plain")          # operator warm-up
    assert (await boxes.for_project("plain")).id == "p-plain"


async def test_separate_from_the_same_image(db):
    await add_project(db, "mine")
    await add_project(db, "arm")
    await placement.put(db, "mine", {"mode": "own", "image": "e2epip"})
    src = await boxes.for_project("mine")
    # the picker's "Separate: new box from the same image": own + that image
    await placement.put(db, "arm", {"mode": "own", "image": src.image[0],
                                    "runtime": src.runtime, "mem_mb": 512})
    b = await boxes.for_project("arm")
    assert b.id == "p-arm" and b is not src
    assert b.image[0] == "e2epip" and b.runtime == src.runtime and b.mem_mb == 512


async def test_change_applies_from_the_next_turn(db):
    await add_project(db, "a")
    b = await boxes.for_project("a")
    assert b.id == "shared"
    boxes.bind_op("op1", b, "a")                        # a turn is running
    await placement.put(db, "a", {"mode": "own"})
    assert (await boxes.for_project("a")).id == "shared"   # nested/concurrent: same box
    boxes.unbind_op("op1")
    assert (await boxes.for_project("a")).id == "p-a"      # next turn


async def test_caps_are_respected_with_a_clear_error(db, monkeypatch):
    monkeypatch.setattr(settings, "vm_max_project_boxes", 1)
    await add_project(db, "a")
    await add_project(db, "b")
    await placement.put(db, "a", {"mode": "own"})
    a = await boxes.for_project("a")
    boxes.bind_op("op-a", a, "a")                       # busy: cannot give way
    out = await placement.put(db, "b", {"mode": "own"})
    assert any("cap" in w for w in out["warnings"])
    with pytest.raises(boxes.BoxCapError, match="project box cap"):
        await boxes.for_project("b")
    # the RAM budget too
    boxes.unbind_op("op-a")
    monkeypatch.setattr(settings, "vm_max_project_boxes", 4)
    a.ctl = _Ctl(running=True)
    a.ctl.inflight = 1
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb",
                        boxes.budget()["ram_mb_used"] + 100)
    with pytest.raises(boxes.BoxCapError, match="RAM budget"):
        await boxes.for_project("b")


async def test_docker_own_refused_when_docker_is_off(db, monkeypatch):
    monkeypatch.setattr(settings, "docker_enabled", False)
    await add_project(db, "a")
    with pytest.raises(placement.PlacementError) as e:
        await placement.put(db, "a", {"mode": "own", "runtime": "docker"})
    assert e.value.status == 409


# --- join -------------------------------------------------------------------------

async def test_join_runs_in_the_owners_box_and_raises_events(db):
    await add_project(db, "mc")
    await add_project(db, "arm")
    await placement.put(db, "mc", {"mode": "own"})
    owner_box = await boxes.for_project("mc")
    out = await placement.put(db, "arm", {"mode": "join:p-mc"}, actor="grindlewalt")
    assert placement.JOIN_WARNING in out["warnings"]
    ev = await events(db, "box_joined")
    assert len(ev) == 1 and ev[0]["project_slug"] == "arm"
    assert ev[0]["detail"]["owner"] == "mc" and ev[0]["detail"]["actor"] == "grindlewalt"
    assert len(await events(db, "placement_changed")) == 2
    assert reviewer.never_auto("box_joined") and reviewer.never_auto("placement_changed")
    b = await boxes.for_project("arm")
    assert b is owner_box and b.joined == {"arm"}
    assert b.to_json()["joined"] == ["arm"]
    # setting the same join again is not a second join event
    await placement.put(db, "arm", {"mode": "join", "box_id": "p-mc"})
    assert len(await events(db, "box_joined")) == 1


async def test_join_rules(db, monkeypatch):
    await add_project(db, "mc")
    await add_project(db, "arm")
    await add_project(db, "third")
    # nothing to join: no box and mc does not run in its own
    with pytest.raises(placement.PlacementError) as e:
        await placement.put(db, "arm", {"mode": "join:p-mc"})
    assert e.value.status == 409
    # a project that does not exist
    with pytest.raises(placement.PlacementError) as e:
        await placement.put(db, "arm", {"mode": "join:p-ghost"})
    assert e.value.status == 404
    # a service box is not a turn box
    with pytest.raises(placement.PlacementError):
        await placement.put(db, "arm", {"mode": "join:s-mc"})
    # no chains: join the box the owner itself joined
    await placement.put(db, "mc", {"mode": "own"})
    await placement.put(db, "arm", {"mode": "join:p-mc"})
    boxes.allocate("project", project="arm")
    with pytest.raises(placement.PlacementError, match="join that box"):
        await placement.put(db, "third", {"mode": "join:p-arm"})
    # boxes off: nothing can be joined
    monkeypatch.setattr(settings, "vm_boxes_enabled", False)
    with pytest.raises(placement.PlacementError):
        await placement.put(db, "third", {"mode": "join:p-mc"})


async def test_join_recreates_a_reaped_box_or_refuses(db):
    await add_project(db, "mc")
    await add_project(db, "arm")
    await placement.put(db, "mc", {"mode": "own"})
    await placement.put(db, "arm", {"mode": "join:p-mc"})
    b = await boxes.for_project("arm")                  # made from mc's placement
    assert b.id == "p-mc" and b.project == "mc"
    boxes.registry.release("p-mc")
    await placement.put(db, "mc", {"mode": "shared"})   # mc gave its box up
    with pytest.raises(boxes.BoxError, match="pick another box"):
        await boxes.for_project("arm")


# --- attribution in a joined box ----------------------------------------------------

async def test_joined_box_attributes_by_the_live_turn(db):
    b = boxes.allocate("project", project="mc")
    # not joined: the owner, as before
    assert egress_proxy.attribute(b)["project"] == "mc"
    b.joined.add("arm")
    # joined, no turn live: unattributed (Default profile, no secrets)
    assert egress_proxy.attribute(b)["project"] is None
    boxes.bind_op("op-arm", b, "arm")
    egress.set_context("arm", "op-arm", 7)
    att = egress_proxy.attribute(b)
    assert (att["project"], att["op_id"], att["conversation_id"]) == ("arm", "op-arm", 7)
    # a turn of a second project live at once (never, via wait_turn_slot): fail
    # to unattributed rather than pick one
    boxes.bind_op("op-mc", b, "mc")
    egress.set_context("mc", "op-mc", 8)
    assert egress_proxy.attribute(b)["project"] is None
    egress.clear_context("op-arm")
    boxes.unbind_op("op-arm")
    assert egress_proxy.attribute(b)["project"] == "mc"
    # a turn in ANOTHER box does not count here
    other = boxes.allocate("project", project="zz")
    boxes.bind_op("op-zz", other, "zz")
    egress.set_context("zz", "op-zz", 9)
    assert egress_proxy.attribute(b)["project"] == "mc"


async def test_secrets_are_not_injected_for_unattributed_joined_traffic(db, monkeypatch):
    from backend import secrets as secrets_mod
    monkeypatch.setattr(secrets_mod, "load", lambda: {"K": "hunter2"})
    b = boxes.allocate("project", project="mc")
    b.joined.add("arm")
    att = egress_proxy.attribute(b)             # no live turn in a joined box
    out, refused = await egress_proxy.inject_secrets(db, att["project"], "x.dev",
                                                     "a {{secret:K}}")
    assert out == "a {{secret:K}}" and refused == ["K"]


async def test_one_projects_turns_at_a_time(db, monkeypatch):
    b = boxes.allocate("project", project="mc")
    # not joined: no waiting at all
    boxes.bind_op("op-mc", b, "mc")
    assert await boxes.wait_turn_slot(b, "arm") is b
    b.joined.add("arm")
    monkeypatch.setattr(boxes, "JOIN_WAIT_SECONDS", 0.3)
    with pytest.raises(boxes.BoxError, match="one project's turns at a time"):
        await boxes.wait_turn_slot(b, "arm")
    # the same project's turns never wait on each other
    assert await boxes.wait_turn_slot(b, "mc") is b
    boxes.unbind_op("op-mc")
    assert await boxes.wait_turn_slot(b, "arm") is b


# --- API -------------------------------------------------------------------------

async def test_api_operator_only_and_round_trip(db):
    from backend.auth import hash_password
    from backend.main import app
    await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                     ("operator", hash_password("pw")))
    await db.commit()
    await add_project(db, "mc")
    await add_project(db, "arm")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/projects/arm/placement")).status_code == 401
        r = await c.put("/api/projects/arm/placement", json={"mode": "join:p-mc"})
        assert r.status_code in (401, 403)
        # a device bearer token is not the operator
        r = await c.get("/api/projects/arm/placement",
                        headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401
        await c.post("/api/auth/login", json={"username": "operator", "password": "pw"})
        r = await c.get("/api/projects/arm/placement")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["setting"]["mode"] == "profile" and j["effective"]["mode"] == "shared"
        assert j["boxes"][0]["id"] == "shared" and "arm" in j["boxes"][0]["used_by"]
        assert any(i["name"] == "main" for i in j["images"])
        assert (await c.get("/api/projects/ghost/placement")).status_code == 404
        r = await c.put("/api/projects/mc/placement", json={"mode": "own", "mem_mb": 600})
        assert r.status_code == 200, r.text
        assert r.json()["effective"]["box_id"] == "p-mc"
        await boxes.for_project("mc")
        r = await c.put("/api/projects/arm/placement", json={"mode": "join:p-mc"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["effective"]["mode"] == "join" and j["warnings"]
        row = next(x for x in j["boxes"] if x["id"] == "p-mc")
        assert set(row["used_by"]) == {"mc", "arm"} and row["in_use"]
        assert j["boxes"][0]["id"] in ("shared", "p-mc")
        r = await c.put("/api/projects/arm/placement", json={"mode": "own", "runtime": "lxc"})
        assert r.status_code == 422
        # the /vms row flags nothing pending for a box matching its placement
        r = await c.get("/api/vm/boxes")
        row = next(x for x in r.json()["boxes"] if x["id"] == "p-mc")
        assert row["joined"] == [] and row["image_pending"] is None


# --- the operator's own change is quiet, an agent's is not -----------------------------

async def test_placement_events_are_quiet_only_when_the_operator_route_says_so(db):
    """`actor` is only a label (the default is "operator"), so a call that merely
    passes it still alerts; the route marks the click with by_operator."""
    await add_project(db, "mc")
    await add_project(db, "arm")
    await placement.put(db, "mc", {"mode": "own"})
    await placement.put(db, "arm", {"mode": "join:p-mc"})              # unmarked
    for kind in ("placement_changed", "box_joined"):
        ev = (await events(db, kind))[-1]
        assert not ev["acknowledged"] and ev["actor"] is None, kind
    await placement.put(db, "arm", {"mode": "own"})
    n_changed = len(await events(db, "placement_changed"))
    n_joined = len(await events(db, "box_joined"))
    await placement.put(db, "arm", {"mode": "join:p-mc"}, actor="grindlewalt",
                        by_operator=True)
    changed = (await events(db, "placement_changed"))[n_changed:]
    joined = (await events(db, "box_joined"))[n_joined:]
    assert len(changed) == 1 and len(joined) == 1
    for ev in changed + joined:
        assert ev["acknowledged"] and ev["actor"] == "operator"
        assert ev["quiet"] == "operator"


async def test_the_placement_route_marks_the_operators_click(db):
    from backend.auth import hash_password
    from backend.main import app
    await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                     ("operator", hash_password("pw")))
    await db.commit()
    await add_project(db, "mc")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "pw"})
        r = await c.put("/api/projects/mc/placement", json={"mode": "own", "mem_mb": 600})
        assert r.status_code == 200, r.text
    (ev,) = await events(db, "placement_changed")
    assert ev["acknowledged"] and ev["actor"] == "operator"
