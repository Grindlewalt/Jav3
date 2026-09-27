"""report_harness_fault — the temporary harness self-report channel.

An agent logs when the HARNESS misbehaved (a tool that errored on valid input, a
documented capability that didn't work). The row lands in `harness_faults`, a
low-severity security event mirrors it into the Review Center, and
GET /api/harness_faults lists them. Offline throughout.
"""
import httpx
import pytest

from backend import memory, runtime
from backend.agent.tools import registry
from backend.auth import hash_password
from backend.db import get_db, init_db, open_conversation
from backend.main import app
from backend.memory import ensure_memory_seeds


async def _dispatch_as(cid, project, args):
    """Run the tool the way the broker does: identity restored from the turn,
    never from arguments."""
    ctoks = (runtime.conversation_id.set(cid), runtime.active_project.set(project))
    try:
        return await registry.dispatch("report_harness_fault", args)
    finally:
        runtime.conversation_id.reset(ctoks[0])
        runtime.active_project.reset(ctoks[1])


async def test_report_harness_fault_stores_row_and_event(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        cid = await open_conversation(db, project=None, title="a turn", kind="agent")
    finally:
        await db.close()

    ack = await _dispatch_as(
        cid, None,
        {"what_i_tried": "send_message to a sibling plan item to coordinate",
         "what_went_wrong": "roster listed only the operator chat, no item: addresses",
         "what_i_expected": "the sibling item:<id> addresses",
         "severity": "medium"})
    assert not ack.startswith("error:"), ack
    assert "logged harness fault #" in ack and "route around it" in ack

    db = await get_db()
    try:
        async with db.execute(
            "SELECT conversation_id, tool, tried, went_wrong, expected, severity "
            "FROM harness_faults") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT kind, severity, summary FROM security_events "
            "WHERE kind = 'harness_fault'") as cur:
            events = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()

    assert len(rows) == 1
    assert rows[0]["conversation_id"] == cid, "identity came from the turn envelope"
    assert rows[0]["severity"] == "medium"
    assert "roster listed only" in rows[0]["went_wrong"]
    assert rows[0]["expected"] == "the sibling item:<id> addresses"
    assert len(events) == 1
    assert events[0]["severity"] == "info", "low/medium faults are info-level events"
    assert "Harness fault reported" in events[0]["summary"]


async def test_report_harness_fault_requires_the_two_core_fields(tmp_env):
    await init_db()
    out = await _dispatch_as(None, None,
                             {"what_i_tried": "", "what_went_wrong": "x"})
    assert out.startswith("error:") and "what_i_tried" in out
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) AS c FROM harness_faults") as cur:
            assert (await cur.fetchone())["c"] == 0
    finally:
        await db.close()


async def test_harness_faults_endpoint_lists_recent(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    await _dispatch_as(None, None,
                       {"what_i_tried": "edit_file", "what_went_wrong": "boom"})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/harness_faults")).status_code == 401
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        r = await c.get("/api/harness_faults")
        assert r.status_code == 200, r.text
        faults = r.json()["faults"]
    assert len(faults) == 1 and faults[0]["tried"] == "edit_file"


def test_harness_spec_is_in_the_system_prompt(tmp_env):
    """The concise 'how this harness works' spec rides the static behavior block
    that assemble_system_prompt always includes."""
    spec = memory.STATIC_BEHAVIOR
    assert "How this harness works" in spec
    assert "read-before-edit" in spec
    assert "offset/limit" in spec
    assert "item:<id>" in spec
    assert "report_harness_fault" in spec
