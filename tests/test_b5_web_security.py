"""Backlog B5 (web security pages): the API halves of WEB-04, WEB-08, WEB-14,
WEB-17 and WEB-20. The page copy itself is in frontend/src/securityCopy.js and
its node test."""
import httpx
import pytest

from backend import egress
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app


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
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


@pytest.fixture
async def db(tmp_env):
    await init_db()
    conn = await get_db()
    yield conn
    await conn.close()


# --- WEB-04: the Network header counts match the log ---------------------------

async def _event(db, host, verdict, slug="proj"):
    await egress.record_event(db, slug=slug, host=host, verdict=verdict)


async def test_summary_counts_hosts_you_approved_as_allowed(client, db):
    await _event(db, "deltamath.com", "deny")
    await egress.note_approved(db, "proj", "deltamath.com")
    await _event(db, "pypi.org", "deny")
    await egress.note_approved(db, "proj", "pypi.org", by="reviewer")
    await _event(db, "odd.example", "deny")          # still blocked
    s = (await client.get("/api/egress/summary")).json()
    # both approvals are allowed hosts now; the deny they answered no longer
    # counts as blocked, so the two numbers add up to the hosts in the log
    assert s["allowed"] == 2 and s["denied"] == 1


async def test_summary_approval_in_another_project_does_not_unblock(client, db):
    await _event(db, "deltamath.com", "deny", slug="a")
    await egress.note_approved(db, "b", "deltamath.com")
    s = (await client.get("/api/egress/summary")).json()
    assert s["denied"] == 1 and s["allowed"] == 1
    s = (await client.get("/api/egress/summary", params={"project": "a"})).json()
    assert s["denied"] == 1 and s["allowed"] == 0


# --- WEB-07: "Allow once" is an hour, on no list, and the host comes back ------

async def test_allow_once_is_time_boxed_and_writes_no_list(client, db):
    await db.execute("INSERT INTO projects (slug, name, path) VALUES ('proj','Proj','p')")
    await db.commit()
    await egress.note_denied(db, "proj", "odd.example")
    pid = (await client.get("/api/egress/pending")).json()["pending"][0]["id"]
    r = await client.post(f"/api/egress/pending/{pid}/approve", json={"once": True})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["project"] == "proj"
    # through for now, on the project's own list: no
    assert (await egress.decide(db, "proj", "odd.example"))[0] == "allow"
    pol = (await client.get("/api/egress/policy/proj")).json()
    assert "odd.example" not in pol["project_allow"]
    # off the queue, and one entry in the standing list marked as a one-off
    assert (await client.get("/api/egress/pending")).json()["pending"] == []
    grp = {g["project"]: g for g in (await client.get("/api/egress/allowlist")).json()["groups"]}
    once = [e for e in grp["proj"]["entries"] if e.get("rule") == "once"]
    assert len(once) == 1 and once[0]["source"] == "auto"
    # the operator's one-offs never use up auto mode's daily cap
    assert await egress.auto_allows_today(db, "proj") == 0
    # when the hour is up it is denied and queued again
    await db.execute("UPDATE egress_auto_allow SET expires_at = datetime('now', '-1 minute')")
    await db.commit()
    assert (await egress.decide(db, "proj", "odd.example"))[1] == egress.NOT_LISTED
    await egress.note_denied(db, "proj", "odd.example")
    assert [p["host"] for p in (await client.get("/api/egress/pending")).json()["pending"]] \
        == ["odd.example"]


async def test_allow_once_still_needs_a_project_for_unattributed_rows(client, db):
    await egress.note_denied(db, egress.GENERAL, "odd.example")
    pid = (await client.get("/api/egress/pending")).json()["pending"][0]["id"]
    r = await client.post(f"/api/egress/pending/{pid}/approve", json={"once": True})
    assert r.status_code == 409 and r.json()["detail"] == "needs_project"


# --- WEB-08: the host's own address is not a site you can allow ----------------

@pytest.fixture
def fake_host(monkeypatch):
    import ipaddress
    from backend import lanaccess
    monkeypatch.setattr(lanaccess, "_host_view", lambda: (
        frozenset({"10.0.0.82", "10.201.0.1"}),
        (ipaddress.ip_network("10.201.0.0/16"),)))


@pytest.mark.parametrize("host,kind", [
    ("10.201.0.1", "host"), ("10.0.0.82", "host"), ("127.0.0.1", "host"),
    ("[::1]", "host"), ("10.0.0.60", "lan"), ("192.168.1.10", "lan"),
    ("8.8.8.8", None), ("pypi.org", None), ("nas.lan", None),
])
def test_unreachable_reason(fake_host, host, kind):
    why = egress.unreachable_reason(host)
    assert why is None if kind is None else why == {
        "host": egress.HOST_REFUSED, "lan": egress.PRIVATE_REFUSED}[kind]


def _att():
    return {"project": "proj", "op_id": None, "conversation_id": None, "box_id": "b1",
            "service_id": None, "kind": "project", "peer_ip": None, "peer_port": None}


async def test_proxy_denies_the_gateway_without_queueing_it(db, fake_host):
    from backend.vm import egress_proxy as ep
    v, reason, pin = await ep._authorize_target("10.201.0.1", "80", _att())
    assert v == "deny" and pin is None and reason != egress.NOT_LISTED
    assert "Jav3 host" in reason
    await ep._record("10.201.0.1", "GET", "/", 0, 0, v, reason, _att())
    assert await egress.list_pending(db, "proj") == []
    # an ordinary unlisted site is still queued
    v, reason, _ = await ep._authorize_target("odd.example", "443", _att())
    assert reason == egress.NOT_LISTED


async def test_old_queued_gateway_row_cannot_be_approved(client, db, fake_host):
    await egress.note_denied(db, "proj", "10.201.0.1")       # queued before the fix
    await egress.note_denied(db, "proj", "pypi.org")
    rows = {r["host"]: r for r in (await client.get("/api/egress/pending")).json()["pending"]}
    assert rows["10.201.0.1"]["refused"] == egress.HOST_REFUSED
    assert rows["pypi.org"]["refused"] is None
    r = await client.post(f"/api/egress/pending/{rows['10.201.0.1']['id']}/approve")
    assert r.status_code == 400 and "Jav3 host" in r.json()["detail"]
    # Approve all adds pypi.org and leaves the gateway alone
    r = await client.post("/api/egress/pending/bulk", json={"action": "approve"})
    assert r.json() == {"ok": True, "done": 1, "skipped": 1}
    pol = (await client.get("/api/egress/policy/proj")).json()
    assert "10.201.0.1" not in pol["project_allow"] and "pypi.org" in pol["project_allow"]
