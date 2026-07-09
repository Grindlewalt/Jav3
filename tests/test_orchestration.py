"""Self-serve agents and schedules, gated: create_agent is live (a definition
executes nothing by itself), schedule_update proposals land PAUSED with
pending_approval=1 and only the operator's toggle settles them. Plus the
round-1 triage note that puts the orchestrate-or-not fork in context before
the first model call."""
import importlib.util as iu

import httpx
import pytest

from backend import runtime
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


def _handler(name):
    spec = iu.spec_from_file_location(
        f"t_{name}", settings.base_dir / "tools" / name / "handler.py")
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
        yield c


# --- create_agent -------------------------------------------------------------

async def test_create_agent_creates_live_roster_entry(tmp_env):
    mod = _handler("create_agent")
    out = await mod.run(name="News Scout", prompt="You are a news scout.",
                        description="morning headlines")
    assert "created agent 'news-scout'" in out
    path = settings.agents_dir / "news-scout" / "AGENT.md"
    assert path.is_file()
    from backend.agents_api import _read
    agent = _read("news-scout")
    assert agent["name"] == "News Scout"
    assert agent["description"] == "morning headlines"
    assert agent["prompt"] == "You are a news scout."

    dup = await mod.run(name="News Scout", prompt="another")
    assert dup.startswith("error:") and "already exists" in dup
    bad = await mod.run(name="!!!", prompt="x")
    assert bad.startswith("error:")
    empty = await mod.run(name="Thing", prompt="  ")
    assert empty.startswith("error:")


async def test_create_agent_blocked_in_incognito(tmp_env):
    mod = _handler("create_agent")
    tok = runtime.ephemeral.set(True)
    try:
        out = await mod.run(name="Sneaky", prompt="x")
    finally:
        runtime.ephemeral.reset(tok)
    assert out.startswith("error:") and "incognito" in out
    assert not (settings.agents_dir / "sneaky").exists()


# --- schedule_update: propose -> operator decides -----------------------------

async def test_schedule_proposal_lands_paused_and_pending(client):
    mod = _handler("schedule_update")
    out = await mod.run(action="create", name="Morning news",
                        task="Summarize today's headlines.",
                        cadence="daily", daily_at="07:30")
    assert "PAUSED" in out and "approval" in out
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM schedules WHERE name = 'Morning news'") as cur:
            row = dict(await cur.fetchone())
    finally:
        await db.close()
    assert row["enabled"] == 0 and row["pending_approval"] == 1
    assert row["daily_at"] == "07:30" and row["next_run"]

    # the bell surfaces it
    r = await client.get("/api/notifications")
    body = r.json()
    assert any(s["name"] == "Morning news" for s in body["schedules"])
    assert body["count"] >= 1

    # operator resumes = approves: enabled, no longer pending, bell clears
    await client.patch(f"/api/schedules/{row['id']}?enabled=true")
    db = await get_db()
    try:
        async with db.execute("SELECT enabled, pending_approval FROM schedules "
                              "WHERE id = ?", (row["id"],)) as cur:
            row2 = dict(await cur.fetchone())
    finally:
        await db.close()
    assert row2 == {"enabled": 1, "pending_approval": 0}
    r = await client.get("/api/notifications")
    assert r.json()["schedules"] == []


async def test_schedule_pause_also_settles_a_proposal(client):
    mod = _handler("schedule_update")
    await mod.run(action="create", name="Parked", task="t")
    db = await get_db()
    try:
        async with db.execute("SELECT id FROM schedules WHERE name = 'Parked'") as cur:
            sid = (await cur.fetchone())["id"]
    finally:
        await db.close()
    await client.patch(f"/api/schedules/{sid}?enabled=false")
    r = await client.get("/api/notifications")
    assert r.json()["schedules"] == []


async def test_schedule_create_validation(tmp_env):
    await init_db()
    mod = _handler("schedule_update")
    assert (await mod.run(action="create", name="", task="t")).startswith("error:")
    assert "HH:MM" in await mod.run(action="create", name="n", task="t",
                                    daily_at="25:99")
    assert "15" in await mod.run(action="create", name="n", task="t",
                                 cadence="interval", interval_minutes=5)
    assert "agent_slug" in await mod.run(action="create", name="n", task="t",
                                         kind="agent")
    missing = await mod.run(action="create", name="n", task="t",
                            kind="agent", agent_slug="ghost")
    assert "create_agent" in missing
    assert (await mod.run(action="create", name="n", task="t",
                          cadence="hourly")).startswith("error:")
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) AS n FROM schedules") as cur:
            assert (await cur.fetchone())["n"] == 0
    finally:
        await db.close()


async def test_schedule_delete_only_retracts_pending(client):
    mod = _handler("schedule_update")
    await mod.run(action="create", name="Mine", task="t")
    db = await get_db()
    try:
        async with db.execute("SELECT id FROM schedules WHERE name = 'Mine'") as cur:
            sid = (await cur.fetchone())["id"]
    finally:
        await db.close()
    # an approved schedule can't be deleted from inside a turn
    await client.patch(f"/api/schedules/{sid}?enabled=true")
    refused = await mod.run(action="delete", id=sid)
    assert refused.startswith("error:") and "GUI" in refused
    # but pausing it is fine (shrinks autonomy)
    assert "paused" in await mod.run(action="disable", id=sid)
    # a still-pending proposal is retractable
    await mod.run(action="create", name="Oops", task="t")
    lst = await mod.run(action="list")
    assert "Oops" in lst and "awaiting approval" in lst
    db = await get_db()
    try:
        async with db.execute("SELECT id FROM schedules WHERE name = 'Oops'") as cur:
            oid = (await cur.fetchone())["id"]
    finally:
        await db.close()
    assert f"retracted pending schedule #{oid}" == await mod.run(action="delete", id=oid)


async def test_schedule_update_blocked_in_incognito(tmp_env):
    await init_db()
    mod = _handler("schedule_update")
    tok = runtime.ephemeral.set(True)
    try:
        out = await mod.run(action="create", name="n", task="t")
    finally:
        runtime.ephemeral.reset(tok)
    assert out.startswith("error:") and "incognito" in out


# --- exclusions: only the conversation head mints infrastructure ---------------

async def test_subagents_cannot_mint_agents_or_schedules(tmp_env):
    from backend import agents_run
    names = {s["function"]["name"]
             for s in agents_run._agent_tools({"tools_exclude": []})}
    assert not names & {"spawn_agent", "deploy_agents",
                        "create_agent", "schedule_update"}


async def test_projectless_chat_grants_planning_tools():
    from backend.chat import ARTIFACT_TOOLS
    assert {"todo_update", "deploy_agents"} <= ARTIFACT_TOOLS


# --- round-1 triage note --------------------------------------------------------

class _Scripted:
    def __init__(self):
        self.seen = []

    async def complete(self, messages, tools=None, **kw):
        self.seen.append([str(m.get("content")) for m in messages])
        yield {"type": "message", "content": "done", "tool_calls": [],
               "usage": None}


def _specs(*names):
    return [{"type": "function", "function": {"name": n, "parameters": {}}}
            for n in names]


async def _first_call_msgs(monkeypatch, tools, self_check=True):
    from backend.agent import loop as loop_mod
    from backend.agent.tools import registry
    model = _Scripted()
    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.commit()
        async for _ in loop_mod.run_turn(
                db, cid, "system", [{"role": "user", "content": "big task"}],
                tools=tools, self_check=self_check):
            pass
    finally:
        await db.close()
    return model.seen[0]


async def test_triage_note_rides_first_user_turn(tmp_env, monkeypatch):
    await init_db()
    msgs = await _first_call_msgs(
        monkeypatch, _specs("todo_update", "research", "spawn_agent"))
    user = msgs[-1]
    assert "[triage —" in user
    assert "todo_update" in user and "research" in user and "spawn_agent" in user
    assert "deploy_agents" not in user   # only offers tools actually granted


async def test_triage_note_skipped_for_subagents_and_no_plan_tool(tmp_env, monkeypatch):
    await init_db()
    msgs = await _first_call_msgs(
        monkeypatch, _specs("todo_update", "research"), self_check=False)
    assert "[triage —" not in msgs[-1]
    msgs = await _first_call_msgs(monkeypatch, _specs("research", "web_read"))
    assert "[triage —" not in msgs[-1]
    msgs = await _first_call_msgs(monkeypatch, _specs("todo_update", "read_file"))
    assert "[triage —" not in msgs[-1]   # a plan with no delegates isn't the fork
