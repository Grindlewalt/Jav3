"""WP2 migration: per-project modes/hosts -> security profiles, in one
transaction, with IDENTICAL verdicts on day one.

`old_decide` below is the pre-profiles egress.decide (commit 968736d), kept
verbatim in logic and run against the pre-migration rows. The test builds a
realistic database (trained general list, every mode, auto-allows live and
expired, a cut, a hidden artifact project, a policy row with no project), takes
the whole (slug x host) verdict matrix BEFORE, migrates, and requires the new
egress.decide to give the same verdict for every cell."""
import json

import pytest

from backend import db as db_mod
from backend import egress, profiles


# --- the pre-profiles decision (reference) -----------------------------------------

def _match(host, patterns):
    host = (host or "").lower().rstrip(".")
    return any(p and (host == p or host.endswith("." + p))
               for p in ((p or "").lower().strip() for p in patterns))


async def old_decide(db, slug, host, cut):
    if (slug, host) in cut or (egress.GENERAL, host) in cut:
        return "cut"
    async with db.execute("SELECT * FROM egress_policy WHERE project_slug = ?",
                          (egress.GENERAL,)) as cur:
        g = await cur.fetchone()
    gen_hosts = json.loads(g["hosts"]) if g else []
    own = None
    if slug and slug != egress.GENERAL:
        async with db.execute("SELECT * FROM egress_policy WHERE project_slug = ?",
                              (slug,)) as cur:
            own = await cur.fetchone()
    if own is None:
        mode, effective = "allowlist", gen_hosts
    else:
        hosts = json.loads(own["hosts"] or "[]")
        mode = own["mode"]
        effective = (hosts + gen_hosts if (own["inherit_general"] and mode == "allowlist")
                     else hosts)
    if mode == "denyall":
        return "deny"
    if mode == "denylist":
        return "deny" if _match(host, effective) else "allow"
    if _match(host, effective):
        return "allow"
    async with db.execute(
            "SELECT 1 FROM egress_auto_allow WHERE project_slug = ? AND host = ? AND "
            "revoked_at IS NULL AND promoted_at IS NULL AND expires_at > datetime('now')",
            (slug, (host or "").lower())) as cur:
        if await cur.fetchone():
            return "allow"
    return "deny"


# --- fixtures ------------------------------------------------------------------------

GENERAL_HOSTS = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
                 "github.com", "api.github.com", "deb.debian.org",
                 "trained-by-approval.dev", "cdn.jsdelivr.net"]

POLICIES = [  # (slug, mode, inherit_general, hosts, has_projects_row, hidden)
    ("webapp", "allowlist", 1, ["api.stripe.com", "hooks.slack.com"], True, 0),
    ("finance", "allowlist", 0, ["internal.api", "bank.example"], True, 0),
    ("research", "denylist", 0, ["ads.tracker", "doubleclick.net", "pypi.org"], True, 0),
    ("vault", "denyall", 0, ["pypi.org", "github.com"], True, 0),
    ("emptyscoped", "allowlist", 0, [], True, 0),
    ("chat-12", "allowlist", 1, ["gist.github.com"], True, 1),       # artifact store
    ("gone-proj", "denylist", 0, ["evil.dev"], False, 0),            # no projects row
    ("gone-scoped", "allowlist", 0, ["only.dev"], False, 0),
]
PLAIN_PROJECTS = ["plain", "other"]                                  # no policy row

SLUGS = [None, egress.GENERAL, "unknown-slug", *PLAIN_PROJECTS,
         *[p[0] for p in POLICIES]]

HOSTS = ["pypi.org", "PYPI.ORG", "pypi.org.", "x.pypi.org", "pypi.org.evil.com",
         "evilpypi.org", "files.pythonhosted.org", "github.com", "gist.github.com",
         "api.github.com", "trained-by-approval.dev", "api.stripe.com",
         "hooks.slack.com", "internal.api", "bank.example", "ads.tracker",
         "x.doubleclick.net", "evil.dev", "only.dev", "huggingface.co",
         "expired-auto.dev", "revoked-auto.dev", "cut.dev", "random-new-host.io",
         "203.0.113.9"]

AUTO = [  # (slug, host, expires modifier, revoked)
    ("webapp", "huggingface.co", "+3 days", False),
    ("plain", "huggingface.co", "+3 days", False),
    ("research", "huggingface.co", "+3 days", False),
    ("vault", "huggingface.co", "+3 days", False),
    ("finance", "expired-auto.dev", "-1 minute", False),
    ("webapp", "revoked-auto.dev", "+3 days", True),
    (egress.GENERAL, "huggingface.co", "+3 days", False),
]
CUT = {("webapp", "cut.dev"), (egress.GENERAL, "pypi.org.evil.com")}


async def build(db):
    await db.execute("INSERT INTO egress_policy(project_slug, mode, inherit_general, hosts) "
                     "VALUES (?, 'allowlist', 0, ?)",
                     (egress.GENERAL, json.dumps(GENERAL_HOSTS)))
    for slug, mode, inh, hosts, has_row, hidden in POLICIES:
        await db.execute("INSERT INTO egress_policy(project_slug, mode, inherit_general, hosts) "
                         "VALUES (?,?,?,?)", (slug, mode, inh, json.dumps(hosts)))
        if has_row:
            await db.execute("INSERT INTO projects(slug, name, path, is_hidden) "
                             "VALUES (?,?,?,?)", (slug, slug, f"/tmp/{slug}", hidden))
    for slug in PLAIN_PROJECTS:
        await db.execute("INSERT INTO projects(slug, name, path) VALUES (?,?,?)",
                         (slug, slug, f"/tmp/{slug}"))
    for slug, host, exp, revoked in AUTO:
        await db.execute(
            "INSERT INTO egress_auto_allow(project_slug, host, rule, reason, expires_at, "
            "revoked_at) VALUES (?, ?, 'known', 't', datetime('now', ?), ?)",
            (slug, host, exp, "2026-01-01" if revoked else None))
    await db.commit()


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    egress._cut.clear()
    conn = await db_mod.get_db()
    yield conn
    await conn.close()
    egress._cut.clear()


async def test_migration_gives_identical_verdicts(db):
    await build(db)
    async with db.execute("SELECT COUNT(*) AS n FROM security_profiles") as cur:
        assert (await cur.fetchone())["n"] == 0                 # nothing migrated yet
    before = {(s, h): await old_decide(db, s, h, CUT) for s in SLUGS for h in HOSTS}

    detail = await profiles.migrate(db)
    assert detail is not None
    egress._cut.update(CUT)
    after = {(s, h): (await egress.decide(db, s, h))[0] for s in SLUGS for h in HOSTS}

    diffs = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    # the ONE deliberate difference: an auto-allow keyed on the unattributed
    # slug no longer applies — unattributed traffic is judged by the Default
    # profile only (DESIGN-BOXES (c)). Everything else is identical.
    assert diffs == {(egress.GENERAL, "huggingface.co"): ("allow", "deny")}
    # the matrix is not trivially uniform: every verdict kind is exercised
    assert {"allow", "deny", "cut"} <= set(before.values())
    assert len(before) == len(SLUGS) * len(HOSTS)


async def test_migration_shape_and_event(db):
    await build(db)
    detail = await profiles.migrate(db)
    profs = {p["name"]: p for p in await profiles.list_all(db)}
    assert profs["Default"]["allow_hosts"] == GENERAL_HOSTS
    for name in profiles.LEGACY_NAMES:
        assert profs[name]["service_placement"] == "per_project"
        assert profs[name]["box_runtime"] == "kvm"
        assert not profs[name]["builtin"] and profs[name]["auto_handle"]
    # exactly one default, and it is the old shared baseline
    assert [n for n, p in profs.items() if p["is_default"]] == ["Default"]
    async with db.execute("SELECT p.slug, sp.name FROM projects p JOIN security_profiles sp "
                          "ON sp.id = p.profile_id") as cur:
        assigned = {r["slug"]: r["name"] for r in await cur.fetchall()}
    assert assigned == {"webapp": "Default", "finance": "Scoped", "research": "Open",
                        "vault": "Offline", "emptyscoped": "Scoped", "chat-12": "Default",
                        "plain": "Default", "other": "Default"}
    # the row's hosts became the project list: allow or deny by mode
    pol = await egress.get_policy(db, "research")
    assert pol["project_deny"] == ["ads.tracker", "doubleclick.net", "pypi.org"]
    assert pol["project_allow"] == []
    pol = await egress.get_policy(db, "finance")
    assert pol["project_allow"] == ["bank.example", "internal.api"]
    assert (await egress.get_policy(db, "vault"))["project_deny"] == ["github.com", "pypi.org"]
    # orphans resolve through their legacy mode column
    assert (await profiles.for_slug(db, "gone-proj"))["name"] == "Open"
    assert (await profiles.for_slug(db, "gone-scoped"))["name"] == "Scoped"
    assert {o["slug"] for o in detail["orphan_policies"]} == {"gone-proj", "gone-scoped"}

    async with db.execute("SELECT * FROM security_events WHERE kind = 'profiles_migrated'"
                          ) as cur:
        evs = [dict(r) for r in await cur.fetchall()]
    assert len(evs) == 1
    d = json.loads(evs[0]["detail"])
    assert set(d["profiles"]) == set(profiles.LEGACY_NAMES)
    assert {p["slug"]: p["profile"] for p in d["projects"]}["vault"] == "Offline"
    # idempotent: a second run (and the lazy path) does nothing
    assert await profiles.migrate(db) is None
    await profiles.ensure_migrated(db)
    async with db.execute("SELECT COUNT(*) AS n FROM security_profiles") as cur:
        assert (await cur.fetchone())["n"] == 4


async def test_migration_is_one_transaction(db, monkeypatch):
    """A failure half-way leaves NOTHING behind: no profiles, no moved lists,
    no assignments, no event."""
    await build(db)
    from backend import secrets as secrets_mod

    def boom():
        raise RuntimeError("fail after the rows were written")
    monkeypatch.setattr(secrets_mod, "load", boom)       # called just before the event
    with pytest.raises(RuntimeError):
        await profiles.migrate(db)
    async with db.execute("SELECT COUNT(*) AS n FROM security_profiles") as cur:
        assert (await cur.fetchone())["n"] == 0
    async with db.execute("SELECT COUNT(*) AS n FROM projects WHERE profile_id IS NOT NULL"
                          ) as cur:
        assert (await cur.fetchone())["n"] == 0
    async with db.execute("SELECT hosts, deny_hosts FROM egress_policy WHERE "
                          "project_slug = 'research'") as cur:
        r = await cur.fetchone()
    assert json.loads(r["hosts"]) == ["ads.tracker", "doubleclick.net", "pypi.org"]
    assert json.loads(r["deny_hosts"]) == []
    async with db.execute("SELECT COUNT(*) AS n FROM security_events") as cur:
        assert (await cur.fetchone())["n"] == 0
    monkeypatch.undo()
    assert await profiles.migrate(db) is not None           # and it can run cleanly after


async def test_fresh_install_seeds_no_profile_and_first_use_creates_the_safe_default(db):
    assert await profiles.migrate(db) is None                # nothing to migrate
    async with db.execute("SELECT COUNT(*) AS n FROM security_profiles") as cur:
        assert (await cur.fetchone())["n"] == 0              # zero built-ins
    assert await profiles.current_default(db) is None
    # the first use creates the safe default: ask me, shared box, no extras
    assert (await egress.decide(db, "anything", "pypi.org"))[0] == "allow"
    assert (await egress.decide(db, "anything", "evil.example"))[0] == "deny"
    profs = await profiles.list_all(db)
    assert len(profs) == 1 and profs[0]["is_default"] and profs[0]["name"] == "Default"
    assert profiles.setup_choices(profs[0]) == {"network": "ask", "placement": "shared",
                                                "services": False, "packages": False}
    async with db.execute("SELECT triage_verdict FROM security_events "
                          "WHERE kind = 'profile_changed'") as cur:
        assert [r["triage_verdict"] for r in await cur.fetchall()] == ["flag"]


async def test_only_the_legacy_profiles_in_use_are_created(db):
    await db.execute("INSERT INTO projects(slug, name, path) VALUES ('p','p','/tmp/p')")
    await db.execute("INSERT INTO egress_policy(project_slug, mode, inherit_general, hosts) "
                     "VALUES ('p', 'denylist', 0, '[]')")
    await db.commit()
    detail = await profiles.migrate(db)
    assert set(detail["profiles"]) == {"Default", "Open"}
    assert (await profiles.for_slug(db, "p"))["name"] == "Open"
    assert (await profiles.current_default(db))["name"] == "Default"


async def _old_code_install(db):
    """What the old migration left behind: four builtin=1 rows, no marked
    default, projects pointing at them."""
    for name, verdict, off in (("Default", "deny", 0), ("Scoped", "deny", 0),
                               ("Open", "allow", 0), ("Offline", "deny", 1)):
        await db.execute(
            "INSERT INTO security_profiles(name, builtin, default_verdict, network_off, "
            "auto_handle, service_placement, box_runtime) "
            "VALUES (?, 1, ?, ?, 1, 'per_project', 'kvm')", (name, verdict, off))
    await db.execute("INSERT INTO security_profiles(name, service_placement, box_runtime) "
                     "VALUES ('Mine', 'shared', 'kvm')")
    for slug, prof in (("a", "Open"), ("b", "Default"), ("c", "Mine")):
        await db.execute("INSERT INTO projects(slug, name, path, profile_id) VALUES "
                         "(?, ?, '/tmp/x', (SELECT id FROM security_profiles "
                         "WHERE name = ?))", (slug, slug, prof))
    await db.commit()


async def test_old_builtins_are_kept_and_the_default_marked(db):
    await _old_code_install(db)
    assert await profiles.migrate(db) is None
    profs = {p["name"]: p for p in await profiles.list_all(db)}
    assert set(profs) == {"Default", "Scoped", "Open", "Offline", "Mine"}   # nothing deleted
    assert not any(p["builtin"] for p in profs.values())                     # flag cleared
    assert [n for n, p in profs.items() if p["is_default"]] == ["Default"]
    assert profs["Open"]["projects"] == ["a"] and profs["Mine"]["projects"] == ["c"]
    # the old builtins are ordinary now: renamable, and deletable when unused
    await profiles.update(db, profs["Scoped"]["id"], {
        "name": "Strict", "service_placement": "per_project", "box_runtime": "kvm"})
    await profiles.delete(db, profs["Offline"]["id"])
    assert await profiles.by_name(db, "Offline") is None
    # idempotent
    assert await profiles.migrate(db) is None
    assert (await profiles.current_default(db))["name"] == "Default"


async def test_old_install_without_a_row_named_default_marks_the_oldest(db):
    await db.execute("INSERT INTO security_profiles(name, builtin, service_placement, "
                     "box_runtime) VALUES ('A', 0, 'shared', 'kvm'), "
                     "('B', 0, 'shared', 'kvm')")
    await db.commit()
    await profiles.migrate(db)
    assert (await profiles.current_default(db))["name"] == "A"


async def test_startup_call_site_migrates(tmp_env):
    await db_mod.init_db()
    conn = await db_mod.get_db()
    try:
        await _old_code_install(conn)
    finally:
        await conn.close()
    await profiles.migrate_at_startup()
    conn = await db_mod.get_db()
    try:
        async with conn.execute("SELECT COUNT(*) AS n FROM security_profiles "
                                "WHERE builtin = 1") as cur:
            assert (await cur.fetchone())["n"] == 0
        async with conn.execute("SELECT name FROM security_profiles "
                                "WHERE is_default = 1") as cur:
            assert [r["name"] for r in await cur.fetchall()] == ["Default"]
    finally:
        await conn.close()


async def test_startup_on_a_fresh_install_creates_nothing(tmp_env):
    await db_mod.init_db()
    await profiles.migrate_at_startup()
    conn = await db_mod.get_db()
    try:
        async with conn.execute("SELECT COUNT(*) AS n FROM security_profiles") as cur:
            assert (await cur.fetchone())["n"] == 0
    finally:
        await conn.close()
