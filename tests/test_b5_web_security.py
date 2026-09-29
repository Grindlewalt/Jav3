"""Backlog B5 (web security pages): the API halves of WEB-04, WEB-08, WEB-14,
WEB-17 and WEB-20. The page copy itself is in frontend/src/securityCopy.js and
its node test."""
import httpx
import pytest

from backend import egress, security
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
