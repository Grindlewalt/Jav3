"""Purging a project removes what is keyed by its slug, so a project created
later under the same slug starts clean (the Pi showed git request #37 of a
purged project on a new one)."""
import httpx
import pytest

from backend import devicetokens
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
            ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        await c.post("/api/projects", json={"name": "Demo", "summary": "a demo"})
        await c.post("/api/projects", json={"name": "Keep", "summary": "stays"})
        yield c


async def _n(db, sql, *args) -> int:
    async with db.execute(sql, args) as cur:
        return (await cur.fetchone())[0]


async def _seed(db, slug: str, device_id: int) -> None:
    await db.execute("INSERT INTO git_requests (project_slug, message) VALUES (?, 'c')", (slug,))
    await db.execute("INSERT INTO git_requests (project_slug, kind, message, status) "
                     "VALUES (?, 'push', 'p', 'approved')", (slug,))
    await db.execute("INSERT INTO permission_rules (project_slug, tool, prefix) "
                     "VALUES (?, 'run', 'ls')", (slug,))
    await db.execute("INSERT INTO project_secret_grants (project_slug, secret_name) "
                     "VALUES (?, 'TOKEN')", (slug,))
    await db.execute("INSERT INTO egress_policy (project_slug, hosts) VALUES (?, '[\"a.dev\"]')",
                     (slug,))
    await db.execute("INSERT INTO egress_pending (project_slug, host) VALUES (?, 'b.dev')",
                     (slug,))
    await db.execute("INSERT INTO egress_auto_allow (project_slug, host, rule, expires_at) "
                     "VALUES (?, 'c.dev', 'known', datetime('now', '+3 days'))", (slug,))
    await db.execute("INSERT INTO browser_grants (device_id, project, read, act) "
                     "VALUES (?, ?, 1, 1)", (device_id, slug))
    await db.execute("INSERT INTO package_catalogue (project_slug, manager, package, status) "
                     "VALUES (?, 'apt', 'jq', 'pending')", (slug,))
    await db.execute("INSERT INTO package_catalogue (project_slug, manager, package, status) "
                     "VALUES (?, 'apt', 'ripgrep', 'built')", (slug,))
    await db.execute("INSERT INTO project_placement (slug, mode, runtime) "
                     "VALUES (?, 'own', 'docker')", (slug,))
    await db.execute("INSERT INTO schedules (name, kind, project_slug, task, cadence_kind, "
                     "interval_minutes, next_run) VALUES ('s', 'jarvis', ?, 't', 'interval', "
                     "60, '2099-01-01T00:00:00')", (slug,))
    for status in ("approved", "pending", "rejected"):
        await db.execute(
            "INSERT INTO services (project_slug, name, command, placement, status, "
            "desired_state) VALUES (?, ?, '[\"x\"]', 'per_project', ?, 'stopped')",
            (slug, f"svc-{status}", status))
    await db.execute("INSERT INTO service_port_events (service_id, port, bind) "
                     "SELECT id, 80, 'loopback' FROM services WHERE project_slug = ?", (slug,))
    # audit rows the purge must leave alone
    await db.execute("INSERT INTO security_events (kind, project_slug, summary) "
                     "VALUES ('egress_anomaly', ?, 'audit')", (slug,))
    await db.execute("INSERT INTO egress_events (project_slug, host) VALUES (?, 'x.dev')",
                     (slug,))
    await db.commit()


# (label, sql with one ? for the slug): rows that must be gone after a purge
GONE = [
    ("git_requests", "SELECT COUNT(*) FROM git_requests WHERE project_slug = ?"),
    ("permission_rules", "SELECT COUNT(*) FROM permission_rules WHERE project_slug = ?"),
    ("project_secret_grants",
     "SELECT COUNT(*) FROM project_secret_grants WHERE project_slug = ?"),
    ("egress_policy", "SELECT COUNT(*) FROM egress_policy WHERE project_slug = ?"),
    ("egress_pending", "SELECT COUNT(*) FROM egress_pending WHERE project_slug = ?"),
    ("browser_grants", "SELECT COUNT(*) FROM browser_grants WHERE project = ?"),
    ("pending package requests",
     "SELECT COUNT(*) FROM package_catalogue WHERE project_slug = ? AND status = 'pending'"),
    ("project_placement", "SELECT COUNT(*) FROM project_placement WHERE slug = ?"),
    ("live schedules",
     "SELECT COUNT(*) FROM schedules WHERE project_slug = ? AND deleted_at IS NULL"),
    ("services", "SELECT COUNT(*) FROM services WHERE project_slug = ?"),
    ("service_port_events", "SELECT COUNT(*) FROM service_port_events WHERE service_id IN "
                            "(SELECT id FROM services WHERE project_slug = ?)"),
]


async def test_purge_removes_slug_keyed_rows_and_keeps_the_rest(client):
    _tok, dev = await devicetokens.mint("browser", by="operator", scope="browser")
    db = await get_db()
    try:
        await _seed(db, "demo", dev)
        await _seed(db, "keep", dev)
        # a project that joined demo's box, and one that joined another box
        await db.execute("INSERT INTO project_placement (slug, mode, box_id) "
                         "VALUES ('joiner', 'join', 'p-demo')")
        await db.execute("INSERT INTO project_placement (slug, mode, box_id) "
                         "VALUES ('joiner2', 'join', 'p-keep')")
        await db.commit()
        for label, sql in GONE:
            assert await _n(db, sql, "demo") > 0, f"seed missing: {label}"
    finally:
        await db.close()

    await client.delete("/api/projects/demo")
    r = await client.delete("/api/projects/demo/purge")
    assert r.status_code == 200

    db = await get_db()
    try:
        for label, sql in GONE:
            assert await _n(db, sql, "demo") == 0, f"left behind: {label}"
            assert await _n(db, sql, "keep") > 0, f"purge reached another project: {label}"
        # the join onto demo's box is gone, the other join stays
        assert await _n(db, "SELECT COUNT(*) FROM project_placement WHERE slug = 'joiner'") == 0
        assert await _n(db, "SELECT COUNT(*) FROM project_placement WHERE slug = 'joiner2'") == 1
        # schedules are binned (restorable), not deleted
        assert await _n(db, "SELECT COUNT(*) FROM schedules WHERE project_slug = 'demo' "
                            "AND deleted_at IS NOT NULL") == 1
        # decided package requests stay: approved ones are part of an image recipe
        assert await _n(db, "SELECT COUNT(*) FROM package_catalogue WHERE project_slug = 'demo' "
                            "AND status = 'built'") == 1
        # a live auto-allow is revoked, and the row is kept as the cap ledger
        assert await _n(db, "SELECT COUNT(*) FROM egress_auto_allow WHERE project_slug = 'demo' "
                            "AND revoked_at IS NOT NULL") == 1
        assert await _n(db, "SELECT COUNT(*) FROM egress_auto_allow WHERE project_slug = 'keep' "
                            "AND revoked_at IS NULL") == 1
        # the audit trail survives a purge
        assert await _n(db, "SELECT COUNT(*) FROM security_events WHERE project_slug = 'demo' "
                            "AND summary = 'audit'") == 1
        assert await _n(db, "SELECT COUNT(*) FROM egress_events WHERE project_slug = 'demo'") == 1
        # the approved and the pending service went through the normal revoke
        assert await _n(db, "SELECT COUNT(*) FROM security_events WHERE project_slug = 'demo' "
                            "AND kind = 'service_revoked'") == 2
    finally:
        await db.close()


async def test_recreated_project_starts_clean(client):
    db = await get_db()
    try:
        await db.execute("INSERT INTO git_requests (project_slug, message) VALUES ('demo', 'old')")
        await db.commit()
    finally:
        await db.close()
    await client.delete("/api/projects/demo")
    await client.delete("/api/projects/demo/purge")
    r = await client.post("/api/projects", json={"name": "Demo", "summary": "again"})
    assert r.status_code == 200, r.text
    db = await get_db()
    try:
        assert await _n(db, "SELECT COUNT(*) FROM git_requests WHERE project_slug = 'demo'") == 0
    finally:
        await db.close()


async def test_purge_drops_service_snapshots(client):
    snap = settings.vm_dir / "svc" / "demo" / "1"
    snap.mkdir(parents=True)
    (snap / ("0" * 64 + ".tar")).write_bytes(b"x")
    other = settings.vm_dir / "svc" / "keep"
    other.mkdir(parents=True)
    await client.delete("/api/projects/demo")
    await client.delete("/api/projects/demo/purge")
    assert not (settings.vm_dir / "svc" / "demo").exists()
    assert other.exists()


async def test_purge_survives_a_failing_service_revoke(client, monkeypatch):
    from backend.vm import services

    async def boom(*a, **k):
        raise services.ServiceError("box gone", 500)
    monkeypatch.setattr(services, "revoke", boom)
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO services (project_slug, name, command, placement, status, "
            "desired_state) VALUES ('demo', 'bot', '[\"x\"]', 'per_project', 'approved', "
            "'running')")
        await db.commit()
    finally:
        await db.close()
    await client.delete("/api/projects/demo")
    r = await client.delete("/api/projects/demo/purge")
    assert r.status_code == 200
    db = await get_db()
    try:
        assert await _n(db, "SELECT COUNT(*) FROM services WHERE project_slug = 'demo'") == 0
        assert await _n(db, "SELECT COUNT(*) FROM projects WHERE slug = 'demo'") == 0
    finally:
        await db.close()
