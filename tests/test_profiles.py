"""WP2: security profiles and project-level policy (DESIGN-BOXES (c)/(d)).

Decision order, the profiles API (cookie-only, placement/runtime defaults,
the delete/rename/default rules), profile_changed diffs, unattributed traffic under the
Default profile only, per-profile auto_handle in the reviewer and egress auto
mode, the hard never-list, and the secret-grant rule on both the wire and the
web path."""
import json

import httpx
import pytest

from backend import db as db_mod
from backend import egress, egress_auto, profiles, reviewer, secrets, security
from backend.config import settings


@pytest.fixture(autouse=True)
def clean_state():
    egress._stack.clear()
    egress._context.update(egress._EMPTY)
    egress._cut.clear()
    yield
    egress._stack.clear()
    egress._context.update(egress._EMPTY)
    egress._cut.clear()


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


async def add_project(db, slug, profile_name=None):
    await db.execute("INSERT INTO projects(slug, name, path) VALUES (?, ?, ?)",
                     (slug, slug, f"/tmp/{slug}"))
    await db.commit()
    if profile_name:
        p = await profiles.legacy_profile(db, profile_name)
        await profiles.assign(db, slug, p["id"])


async def events(db, kind):
    async with db.execute("SELECT * FROM security_events WHERE kind = ? ORDER BY id",
                          (kind,)) as cur:
        return [security._row(r) for r in await cur.fetchall()]


async def new_profile(db, **kw):
    body = {"name": "Custom", "service_placement": "per_project", "box_runtime": "kvm",
            **kw}
    return await profiles.create(db, body)


# --- the default and the legacy shapes ------------------------------------------------

async def test_legacy_shapes_are_explicit_per_project_kvm(db):
    for name in profiles.LEGACY_NAMES:
        await profiles.legacy_profile(db, name)
    profs = {p["name"]: p for p in await profiles.list_all(db)}
    assert set(profs) == {"Default", "Scoped", "Open", "Offline"}
    assert [n for n, p in profs.items() if p["is_default"]] == ["Default"]
    for name in profiles.LEGACY_NAMES:
        p = profs[name]
        assert not p["builtin"] and p["service_placement"] == "per_project"
        assert p["box_runtime"] == "kvm"
    assert profs["Default"]["default_verdict"] == "deny"
    assert "pypi.org" in profs["Default"]["allow_hosts"]          # the old general seeds
    assert profs["Open"]["default_verdict"] == "allow"
    assert profs["Offline"]["network_off"] is True


async def test_schema_refuses_a_profile_without_placement_or_runtime(db):
    import aiosqlite
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute("INSERT INTO security_profiles(name, box_runtime) VALUES ('x', 'kvm')")
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute("INSERT INTO security_profiles(name, service_placement) "
                         "VALUES ('y', 'shared')")


# --- decision order ----------------------------------------------------------------
# cut -> project deny -> profile deny -> project allow -> profile allow ->
# live auto-allow -> profile default. Each row puts a host on two adjacent
# levels and checks the earlier one wins.

@pytest.mark.parametrize("setup, expect", [
    # (project allow, project deny, profile allow, profile deny, auto, cut) -> verdict
    (dict(cut=True, p_allow=True), "cut"),
    (dict(cut=True, prof_allow=True), "cut"),
    (dict(p_deny=True, prof_allow=True), "deny"),
    (dict(p_deny=True, p_allow=True), "deny"),
    (dict(prof_deny=True, p_allow=True), "deny"),
    (dict(prof_deny=True, auto=True), "deny"),
    (dict(p_allow=True), "allow"),
    (dict(prof_allow=True), "allow"),
    (dict(auto=True), "allow"),
    (dict(), "deny"),
    (dict(default="allow"), "allow"),
    (dict(default="allow", p_deny=True), "deny"),
    (dict(default="allow", prof_deny=True), "deny"),
    (dict(network_off=True, p_allow=True, prof_allow=True, auto=True), "deny"),
    (dict(network_off=True, cut=True), "cut"),
])
async def test_decide_order_matrix(db, setup, expect):
    host = "api.target.dev"
    await add_project(db, "proj")
    prof = await new_profile(
        db, default_verdict=setup.get("default", "deny"),
        network_off=setup.get("network_off", False),
        allow_hosts=["target.dev"] if setup.get("prof_allow") else [],
        deny_hosts=["target.dev"] if setup.get("prof_deny") else [])
    await profiles.assign(db, "proj", prof["id"])
    await egress.set_lists(db, "proj",
                           allow=[host] if setup.get("p_allow") else [],
                           deny=[host] if setup.get("p_deny") else [])
    if setup.get("auto"):
        await egress.add_auto(db, "proj", host, rule="known", reason="test")
    if setup.get("cut"):
        egress.mark_cut("proj", host)
    verdict, reason = await egress.decide(db, "proj", host)
    assert verdict == expect, reason


async def test_decide_reasons_name_the_level(db):
    await add_project(db, "proj")
    prof = await new_profile(db, allow_hosts=["good.dev"], deny_hosts=["bad.dev"])
    await profiles.assign(db, "proj", prof["id"])
    await egress.set_lists(db, "proj", allow=["mine.dev"], deny=["nope.dev"])
    assert (await egress.decide(db, "proj", "nope.dev"))[1] == "host on the project denylist"
    assert "Custom profile denylist" in (await egress.decide(db, "proj", "bad.dev"))[1]
    assert (await egress.decide(db, "proj", "mine.dev"))[1] == "host on the project allowlist"
    assert "Custom profile allowlist" in (await egress.decide(db, "proj", "good.dev"))[1]
    assert (await egress.decide(db, "proj", "x.dev"))[1] == egress.NOT_LISTED


# --- unattributed traffic: the Default profile only -----------------------------------

async def test_unattributed_is_judged_by_default_only(db):
    # a project list or auto-allow keyed on __general__ must never apply
    await db.execute("UPDATE egress_policy SET hosts = ? WHERE project_slug = ?",
                     (json.dumps(["legacy-general-only.dev"]), egress.GENERAL))
    await egress.add_auto(db, egress.GENERAL, "auto.dev", rule="known", reason="x")
    await db.commit()
    for slug in (None, egress.GENERAL):
        assert (await egress.decide(db, slug, "pypi.org"))[0] == "allow"        # Default
        assert (await egress.decide(db, slug, "auto.dev"))[0] == "deny"
        assert (await egress.decide(db, slug, "legacy-general-only.dev"))[0] == "deny"
    pol = await egress.get_policy(db, None)
    assert pol["profile"]["name"] == "Default" and pol["project_allow"] == []
    # egress auto mode never guesses for it
    await egress_auto.set_mode(db, None, "on")
    assert await egress_auto.judge(db, egress.GENERAL, "huggingface.co", 443) is None


async def test_unattributed_approval_needs_a_project(db):
    await add_project(db, "alpha")
    await egress.note_denied(db, egress.GENERAL, "cdn.example.dev", box_id="shared")
    row = (await egress.list_pending(db))[0]
    assert row["box_id"] == "shared"
    res = await egress.approve_host(db, row["id"])
    assert not res["ok"] and res["needs_project"]
    res = await egress.approve_host(db, row["id"], project="alpha")
    assert res["ok"] and res["added_to"] == "alpha"
    assert (await egress.decide(db, "alpha", "cdn.example.dev"))[0] == "allow"
    assert (await egress.decide(db, None, "cdn.example.dev"))[0] == "deny"
    assert not (await egress.allow_host(db, egress.GENERAL, "x.dev"))["ok"]


async def test_bulk_approve_skips_unattributed_rows(db):
    await egress.note_denied(db, egress.GENERAL, "a.dev")
    await egress.note_denied(db, "proj", "b.dev")
    res = await egress.bulk_pending(db, "approve")
    assert res["done"] == 1 and res["skipped"] == 1
    assert [p["host"] for p in await egress.list_pending(db)] == ["a.dev"]


async def test_approval_lifts_the_host_off_the_project_denylist(db):
    await egress.set_lists(db, "proj", deny=["x.dev"])
    await egress.note_denied(db, "proj", "x.dev")
    pid = (await egress.list_pending(db, "proj"))[0]["id"]
    await egress.approve_host(db, pid)
    pol = await egress.get_policy(db, "proj")
    assert pol["project_allow"] == ["x.dev"] and pol["project_deny"] == []
    assert (await egress.decide(db, "proj", "x.dev"))[0] == "allow"


# --- service boxes: deny-by-default on approved hosts only ----------------------------

async def test_service_egress_is_its_approved_hosts_minus_deny_lists(db):
    await add_project(db, "alpha", "Open")          # allow-by-default must NOT apply
    cur = await db.execute(
        "INSERT INTO services(project_slug, name, command, placement, status, "
        "desired_state, egress_hosts) "
        "VALUES ('alpha', 'bot', '[\"x\"]', 'per_service', 'approved', 'running', ?)",
        (json.dumps(["api.telegram.org", "blocked.dev"]),))
    sid = cur.lastrowid
    await db.commit()
    await egress.set_lists(db, "alpha", deny=["blocked.dev"])
    assert (await egress.decide_service(db, "alpha", sid, "api.telegram.org"))[0] == "allow"
    assert (await egress.decide_service(db, "alpha", sid, "pypi.org"))[0] == "deny"
    assert (await egress.decide_service(db, "alpha", sid, "blocked.dev"))[0] == "deny"
    # a stopped service's hosts close (its box-mates cannot borrow them)
    await db.execute("UPDATE services SET desired_state = 'stopped' WHERE id = ?", (sid,))
    await db.commit()
    assert (await egress.decide_service(db, "alpha", sid, "api.telegram.org"))[0] == "deny"
    await db.execute("UPDATE services SET status = 'revoked', desired_state = 'running' "
                     "WHERE id = ?", (sid,))
    await db.commit()
    assert (await egress.decide_service(db, "alpha", sid, "api.telegram.org"))[0] == "deny"


# --- profiles CRUD + events ---------------------------------------------------------

async def test_create_defaults_placement_and_runtime(db):
    # a new profile defaults to the shared box with per-project services
    p = await profiles.create(db, {"name": "P"})
    assert p["service_placement"] == "per_project" and p["box_runtime"] == "kvm"
    assert p["separate_box"] is False
    q = await profiles.create(db, {"name": "Q", "service_placement": "shared",
                                   "separate_box": True, "box_runtime": "docker"})
    assert q["service_placement"] == "shared" and q["box_runtime"] == "docker"
    with pytest.raises(profiles.ProfileError):
        await profiles.create(db, {"name": "P", "service_placement": "nope",
                                   "box_runtime": "kvm"})


async def test_edit_emits_profile_changed_with_diff(db):
    p = await new_profile(db, allow_hosts=["a.dev"])
    await profiles.update(db, p["id"], {"service_placement": "per_project",
                                        "box_runtime": "kvm",
                                        "default_verdict": "allow",
                                        "allow_hosts": ["a.dev", "b.dev"]})
    ev = (await events(db, "profile_changed"))[-1]
    ch = ev["detail"]["changes"]
    assert ch["default_verdict"] == {"from": "deny", "to": "allow"}
    assert ch["allow_hosts"] == {"from": ["a.dev"], "to": ["a.dev", "b.dev"]}
    # widened to allow-by-default: the operator's own click is recorded as a
    # warning, not a critical ping (operator decision 2026-09-29) ...
    assert ev["severity"] == "warn"
    # ... anything else widening it is still critical
    widen = {"default_verdict": {"from": "deny", "to": "allow"}}
    assert profiles._severity(widen, "setup") == "critical"
    assert profiles._severity({"network_off": {"from": True, "to": False}},
                              "legacy policy call") == "critical"
    assert profiles._severity(widen, "operator") == "warn"


async def test_default_cannot_be_deleted_until_another_is_marked(db):
    d = await profiles.default(db)
    with pytest.raises(profiles.ProfileError) as e:
        await profiles.delete(db, d["id"])
    assert e.value.status == 409 and "make another profile the default" in str(e.value)
    # but it can be renamed and edited like any other
    await profiles.update(db, d["id"], {"name": "Home", "service_placement":
                                        "per_project", "box_runtime": "kvm"})
    assert (await profiles.default(db))["name"] == "Home"
    other = await new_profile(db)
    assert not other["is_default"]
    await profiles.set_default(db, other["id"])
    assert (await profiles.default(db))["id"] == other["id"]
    ev = (await events(db, "profile_changed"))[-1]
    assert ev["detail"]["action"] == "make_default"
    assert ev["detail"]["from"]["name"] == "Home"
    await profiles.delete(db, d["id"])                  # no longer the default
    assert await profiles.get(db, d["id"]) is None


async def test_exactly_one_default(db):
    import aiosqlite
    a = await new_profile(db, name="A")
    # a profile made on an install with none: the safe default comes first
    assert not a["is_default"] and (await profiles.default(db))["name"] == "Default"
    b = await new_profile(db, name="B")
    c = await profiles.create(db, {"name": "C", "service_placement": "shared",
                                   "box_runtime": "kvm"}, make_default=True)
    assert c["is_default"]
    async with db.execute("SELECT name FROM security_profiles WHERE is_default = 1") as cur:
        assert [r["name"] for r in await cur.fetchall()] == ["C"]
    await profiles.set_default(db, b["id"])
    async with db.execute("SELECT name FROM security_profiles WHERE is_default = 1") as cur:
        assert [r["name"] for r in await cur.fetchall()] == ["B"]
    # the schema refuses a second marked row outright
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute("UPDATE security_profiles SET is_default = 1 WHERE id = ?",
                         (a["id"],))
    await db.rollback()
    with pytest.raises(profiles.ProfileError) as e:
        await profiles.set_default(db, 9999)
    assert e.value.status == 404


async def test_create_rename_delete_rules(db):
    await profiles.default(db)
    p = await new_profile(db, name="Lab")
    p = await profiles.update(db, p["id"], {"name": "Lab 2", "service_placement":
                                            "per_project", "box_runtime": "kvm"})
    assert p["name"] == "Lab 2"
    with pytest.raises(profiles.ProfileError) as e:
        await new_profile(db, name="Lab 2")
    assert e.value.status == 409                      # names stay unique
    await add_project(db, "alpha")
    await profiles.assign(db, "alpha", p["id"])
    with pytest.raises(profiles.ProfileError) as e:
        await profiles.delete(db, p["id"])
    assert e.value.status == 409 and "alpha" in str(e.value)
    await profiles.assign(db, "alpha", (await profiles.default(db))["id"])
    assert (await profiles.delete(db, p["id"]))["ok"]


async def test_unassigned_projects_follow_the_marked_default(db):
    await add_project(db, "alpha")                    # profile_id NULL
    assert (await profiles.for_slug(db, "alpha"))["name"] == "Default"
    open_ = await profiles.create(db, {"name": "Wide", "default_verdict": "allow",
                                       "service_placement": "per_project",
                                       "box_runtime": "kvm"})
    await profiles.set_default(db, open_["id"])
    assert (await profiles.for_slug(db, "alpha"))["name"] == "Wide"
    assert (await profiles.for_slug(db, None))["name"] == "Wide"
    assert (await egress.decide(db, "alpha", "anything.dev"))[0] == "allow"
    ev = (await events(db, "profile_changed"))[-1]
    assert ev["detail"]["projects"] == ["alpha"] and ev["severity"] == "warn"
    listed = {p["name"]: p["projects"] for p in await profiles.list_all(db)}
    assert listed["Wide"] == ["alpha"] and listed["Default"] == []


async def test_assign_emits_profile_changed_and_moves_the_verdicts(db):
    await add_project(db, "alpha")
    assert (await egress.decide(db, "alpha", "anything.dev"))[0] == "deny"
    open_ = await profiles.legacy_profile(db, "Open")
    await profiles.assign(db, "alpha", open_["id"])
    assert (await egress.decide(db, "alpha", "anything.dev"))[0] == "allow"
    ev = (await events(db, "profile_changed"))[-1]
    assert ev["project_slug"] == "alpha"
    assert ev["detail"]["from"]["name"] == "Default" and ev["detail"]["to"]["name"] == "Open"
    assert ev["detail"]["changes"]["default_verdict"] == {"from": "deny", "to": "allow"}
    with pytest.raises(profiles.ProfileError) as e:
        await profiles.assign(db, "no-such-project", open_["id"])
    assert e.value.status == 404


async def test_api_round_trip(tmp_env):
    from backend.auth import hash_password
    from backend.main import app

    await db_mod.init_db()
    conn = await db_mod.get_db()
    await conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                       ("operator", hash_password("pw")))
    await conn.execute("INSERT INTO projects(slug, name, path) VALUES ('alpha','a','/tmp/a')")
    await conn.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/profiles")).status_code == 401          # cookie-only
        await c.post("/api/auth/login", json={"username": "operator", "password": "pw"})
        rows = (await c.get("/api/profiles")).json()["profiles"]
        d = next(p for p in rows if p["name"] == "Default")
        assert "alpha" in d["projects"] and d["service_placement"] == "per_project"
        assert d["box_runtime"] == "kvm"
        base = {"name": "Lab", "default_verdict": "deny", "allow_hosts": ["lab.dev"]}
        # create fills placement/runtime with the form's defaults
        r = await c.post("/api/profiles", json={**base, "name": "Plain"})
        assert r.status_code == 200, r.text
        assert r.json()["service_placement"] == "per_project"
        assert r.json()["box_runtime"] == "kvm"
        r = await c.post("/api/profiles", json={**base, "box_runtime": "kvm",
                                                "service_placement": "per_service"})
        assert r.status_code == 200, r.text
        lab = r.json()
        # an edit must name both again: no silent default
        r = await c.put(f"/api/profiles/{lab['id']}", json={"auto_handle": True})
        assert r.status_code == 422
        # e2e BUG-10: full-row PUT is the contract; the refusal says so
        assert "full row" in r.text
        r = await c.put(f"/api/profiles/{lab['id']}",
                        json={"auto_handle": True, "service_placement": "shared",
                              "box_runtime": "kvm"})
        assert r.status_code == 200 and r.json()["auto_handle"] is True
        assert r.json()["service_placement"] == "shared"
        r = await c.put("/api/projects/alpha/profile", json={"profile_id": lab["id"]})
        assert r.status_code == 200 and r.json()["profile"]["name"] == "Lab"
        pol = (await c.get("/api/egress/policy/alpha")).json()
        assert pol["profile"] == {"id": lab["id"], "name": "Lab", "default": "deny",
                                  "network_off": False, "is_default": False}
        r = await c.put("/api/egress/policy/alpha", json={"allow": ["own.dev"],
                                                          "deny": ["lab.dev"]})
        assert r.status_code == 200
        pol = (await c.get("/api/egress/policy/alpha")).json()
        assert pol["project_allow"] == ["own.dev"] and pol["project_deny"] == ["lab.dev"]
        assert set(pol["effective_allow"]) == {"own.dev", "lab.dev"}
        assert pol["effective_deny"] == ["lab.dev"]
        assert (await c.put("/api/egress/policy/alpha", json={"allow": ["http://x/y"]})
                ).status_code == 400
        groups = (await c.get("/api/egress/allowlist")).json()["groups"]
        kinds = [g["kind"] for g in groups]
        assert kinds.index("project") < kinds.index("profile")          # project, then profile
        alpha = next(g for g in groups if g["project"] == "alpha")
        assert alpha["profile"]["name"] == "Lab" and alpha["deny"] == ["lab.dev"]
        # promote: one call moves a host from the project list to the profile
        r = await c.post("/api/egress/policy/alpha/promote", json={"host": "own.dev"})
        assert r.status_code == 200 and r.json()["profile"]["name"] == "Lab"
        pol = (await c.get("/api/egress/policy/alpha")).json()
        assert pol["project_allow"] == [] and "own.dev" in pol["effective_allow"]
        lab_row = next(p for p in (await c.get("/api/profiles")).json()["profiles"]
                       if p["id"] == lab["id"])
        assert "own.dev" in lab_row["allow_hosts"]
        assert (await c.delete(f"/api/profiles/{lab['id']}")).status_code == 409  # in use
        r = await c.delete(f"/api/profiles/{d['id']}")
        assert r.status_code == 409 and "default" in r.json()["detail"]      # the default
        assert d["is_default"] and "builtin" not in d
        await c.put("/api/projects/alpha/profile", json={"profile_id": d["id"]})
        assert (await c.delete(f"/api/profiles/{lab['id']}")).status_code == 200
        # Make default moves the mark; then the old default can go
        plain = next(p for p in (await c.get("/api/profiles")).json()["profiles"]
                     if p["name"] == "Plain")
        r = await c.post(f"/api/profiles/{plain['id']}/default")
        assert r.status_code == 200 and r.json()["is_default"] is True
        rows = (await c.get("/api/profiles")).json()["profiles"]
        assert [p["name"] for p in rows if p["is_default"]] == ["Plain"]
        assert (await c.post("/api/profiles/9999/default")).status_code == 404
        await c.put("/api/projects/alpha/profile", json={"profile_id": plain["id"]})
        assert (await c.delete(f"/api/profiles/{d['id']}")).status_code == 200
        # an unattributed queue row needs a project on approval
        await egress.note_denied(conn, egress.GENERAL, "q.dev")
        pid = (await egress.list_pending(conn))[0]["id"]
        r = await c.post(f"/api/egress/pending/{pid}/approve")
        assert r.status_code == 409 and r.json()["detail"] == "needs_project"
        g = (await c.get("/api/egress/policy/__general__")).json()
        assert g["profile"]["name"] == "Plain" and g["source"] == "general"
        assert (await c.put("/api/egress/policy/__general__", json={"allow": ["x.dev"]})
                ).status_code == 400
        assert (await c.put("/api/egress/policy/__image_build__", json={"allow": ["x.dev"]})
                ).status_code == 400
        r = await c.post(f"/api/egress/pending/{pid}/approve", json={"project": "alpha"})
        assert r.status_code == 200 and r.json()["added_to"] == "alpha"
    await conn.close()


# --- reviewer: per-profile auto_handle + the never-list ---------------------------------

def fake_model(verdicts):
    calls = []

    async def _fake(system, user, temperature=0.3):
        calls.append(user)
        return json.dumps(verdicts)

    _fake.calls = calls
    return _fake


async def test_reviewer_skips_projects_whose_profile_is_off(db, monkeypatch):
    await add_project(db, "locked")
    off = await new_profile(db, auto_handle=False)
    await profiles.assign(db, "locked", off["id"])
    await db.execute("INSERT INTO egress_pending(project_slug, host) VALUES "
                     "('locked', 'registry.example.dev'), ('proj', 'other.example.dev')")
    await db.commit()
    await security.raise_event(db, kind="gate_flag", severity="warn", project="locked",
                               summary="routine")
    fake = fake_model([])
    monkeypatch.setattr(reviewer, "complete_text", fake)
    await reviewer.run()
    joined = "\n".join(fake.calls)
    assert "other.example.dev" in joined
    assert "registry.example.dev" not in joined and "routine" not in joined
    async with db.execute("SELECT triage_verdict FROM egress_pending WHERE "
                          "project_slug = 'locked'") as cur:
        assert (await cur.fetchone())["triage_verdict"] is None         # untouched
    # egress auto mode is off for it too, whatever the switches say
    await egress_auto.set_mode(db, None, "on")
    await egress_auto.set_mode(db, "locked", "on")
    assert (await egress_auto.get_mode(db, "locked"))["effective"] is False
    assert (await egress_auto.get_mode(db, "proj"))["effective"] is True


@pytest.mark.parametrize("kind", ["service_requested", "service_approved", "svc_unreported",
                                  "package_requested", "package_approved",
                                  "unexpected_process", "proc_report_mismatch",
                                  "profile_changed", "egress_anomaly", "secret_leak"])
async def test_never_list_holds_with_auto_handle_on(db, monkeypatch, kind):
    on = await new_profile(db, auto_handle=True)
    await add_project(db, "p")
    await profiles.assign(db, "p", on["id"])
    eid = await security.raise_event(db, kind=kind, severity="info", project="p",
                                     summary="looks routine")
    # only this alert is waiting (the profile_changed from the assign is itself
    # never-listed; set it aside so the counts below are about `kind`)
    await db.execute("UPDATE security_events SET triage_verdict = 'flag' WHERE id != ?",
                     (eid,))
    await db.commit()
    # the model says ack: the guardrail must win and never spend a token on it
    fake = fake_model([{"id": f"a{eid}", "verdict": "ack", "reason": "fine"}])
    monkeypatch.setattr(reviewer, "complete_text", fake)
    res = await reviewer.run()
    assert res["acked"] == 0 and fake.calls == []
    async with db.execute("SELECT acknowledged, triage_verdict FROM security_events "
                          "WHERE id = ?", (eid,)) as cur:
        r = await cur.fetchone()
    assert r["acknowledged"] == 0 and r["triage_verdict"] == "flag"


async def test_reviewer_never_approves_unattributed_hosts(db, monkeypatch):
    await db.execute("INSERT INTO egress_pending(project_slug, host) VALUES (?, 'x.dev')",
                     (egress.GENERAL,))
    await db.commit()
    fake = fake_model([])
    monkeypatch.setattr(reviewer, "complete_text", fake)
    res = await reviewer.run()
    assert res["allowed"] == 0 and res["flagged"] == 1 and fake.calls == []


# --- secret grants: profile list + project grants, a revoke wins -------------------------

async def test_granted_secrets_rule(db):
    await add_project(db, "alpha")
    prof = await new_profile(db, secrets=["SHARED_KEY", "OTHER_KEY"])
    await profiles.assign(db, "alpha", prof["id"])
    await egress.grant_secret(db, "alpha", "OWN_KEY")
    await egress.revoke_secret(db, "alpha", "OTHER_KEY")        # revoke beats profile
    assert await egress.granted_secrets(db, "alpha") == {"SHARED_KEY", "OWN_KEY"}
    assert await egress.may_use_secret(db, "alpha", "shared_key")
    assert not await egress.may_use_secret(db, "alpha", "OTHER_KEY")
    assert await egress.granted_secrets(db, None) == set()      # Default has none


def test_substitute_url_enforces_grants(tmp_env):
    secrets.save({"NEWS": {"value": "k-123456789", "hosts": ["api.news.dev"]}})
    url = "https://api.news.dev/v1?k={{secret:NEWS}}"
    assert "k-123456789" in secrets.substitute_url(url, granted={"NEWS"}, project="a")
    with pytest.raises(ValueError) as e:
        secrets.substitute_url(url, granted=set(), project="a")
    assert "not granted to project 'a'" in str(e.value)
    # host binding still applies on top of the grant
    with pytest.raises(ValueError):
        secrets.substitute_url("https://evil.dev/?k={{secret:NEWS}}", granted={"NEWS"})


async def test_web_read_refuses_an_ungranted_bound_secret(db, monkeypatch):
    from backend import runtime, webtools
    secrets.save({"NEWS": {"value": "k-123456789", "hosts": ["api.news.dev"]}})
    await add_project(db, "alpha")
    tok = runtime.active_project.set("alpha")
    try:
        out = await webtools.read("https://api.news.dev/v1?k={{secret:NEWS}}", "s")
        assert out.startswith("error:") and "not granted to project 'alpha'" in out
        assert "k-123456789" not in out
        # granted: substitution happens (the fetch itself is stubbed out)
        await egress.grant_secret(db, "alpha", "NEWS")
        seen = []

        def _unsafe(u):
            seen.append(u)
            raise webtools.UnsafeURL("stop before the network")
        monkeypatch.setattr(webtools, "is_safe_url", _unsafe)
        out = await webtools.read("https://api.news.dev/v1?k={{secret:NEWS}}", "s")
        assert seen and "k-123456789" in seen[0]
        assert "k-123456789" not in out
    finally:
        runtime.active_project.reset(tok)


async def test_proxy_injects_a_profile_granted_secret(db):
    from backend.vm import egress_proxy as ep
    secrets.save({"PROF_KEY": "pk-123456789"})
    await add_project(db, "alpha")
    prof = await new_profile(db, secrets=["PROF_KEY"])
    await profiles.assign(db, "alpha", prof["id"])
    out, refused = await ep.inject_secrets(db, "alpha", "api.x.dev",
                                           "GET /?k={{secret:PROF_KEY}} HTTP/1.1\r\n\r\n")
    assert "pk-123456789" in out and refused == []
    await egress.revoke_secret(db, "alpha", "PROF_KEY")
    out, refused = await ep.inject_secrets(db, "alpha", "api.x.dev",
                                           "GET /?k={{secret:PROF_KEY}} HTTP/1.1\r\n\r\n")
    assert refused == ["PROF_KEY"]


async def test_seed_hosts_setting_is_the_default_profile_seed(db):
    d = await profiles.default(db)
    assert sorted(set(settings.egress_seed_hosts)) == d["allow_hosts"]
