"""Convo-33 fixes: delegation nudges mid-turn, wrap-up pressure, conclusion
retry against tool-fixated replies, and activity persisted into the messages
payload so the dropdown survives reloads."""
import asyncio
import json

import httpx
import pytest

from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


class _ScriptedModel:
    def __init__(self, rounds):
        self.rounds = rounds
        self.call = 0
        self.seen = []

    async def complete(self, messages, tools=None, **kw):
        self.seen.append([str(m.get("content")) for m in messages])
        if self.call < len(self.rounds):
            calls = [{"id": f"c{self.call}_{j}", "type": "function",
                      "function": {"name": n, "arguments": a}}
                     for j, (n, a) in enumerate(self.rounds[self.call])]
            self.call += 1
            yield {"type": "message", "content": "", "tool_calls": calls,
                   "usage": None}
        else:
            yield {"type": "message", "content": "done", "tool_calls": [],
                   "usage": None}


async def _run(monkeypatch, model, dispatch, tools=None):
    from backend.agent import loop as loop_mod
    from backend.agent.tools import registry
    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())
    loop_mod._files_seen.clear()
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.commit()
        events = []
        async for ev in loop_mod.run_turn(
                db, cid, "system", [{"role": "user", "content": "go"}],
                tools=tools or [{"type": "function",
                                 "function": {"name": "x", "parameters": {}}}]):
            events.append(ev)
        return events
    finally:
        await db.close()


RESEARCH_TOOLS = [
    {"type": "function", "function": {"name": "research", "parameters": {}}},
    {"type": "function", "function": {"name": "web_read", "parameters": {}}},
]


async def test_delegation_nudge_fires_when_delegate_available(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "delegate_nudge_round", 3)

    async def dispatch(name, args):
        return "some page text"

    model = _ScriptedModel([[("web_read", f'{{"u": "{i}"}}')] for i in range(5)])
    await _run(monkeypatch, model, dispatch, tools=RESEARCH_TOOLS)
    # by the 4th model call the nudge rides the 3rd round's result
    assert any("hand web gathering to the research tool" in m
               for m in model.seen[3])


async def test_no_delegation_nudge_without_delegate_tools(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "delegate_nudge_round", 3)

    async def dispatch(name, args):
        return "ok"

    model = _ScriptedModel([[("x", f'{{"i": {i}}}')] for i in range(5)])
    await _run(monkeypatch, model, dispatch)
    assert not any("research tool" in m for view in model.seen for m in view)


async def test_wrap_up_nudge_at_two_thirds(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "max_react_iterations", 9)
    monkeypatch.setattr(settings, "delegate_nudge_round", 99)

    async def dispatch(name, args):
        return "ok"

    model = _ScriptedModel([[("x", f'{{"i": {i}}}')] for i in range(9)])
    await _run(monkeypatch, model, dispatch)
    # 2/3 of 9 = round 6: the wrap-up note appears in call 7's view
    assert any("start concluding" in m for m in model.seen[6])


async def test_conclusion_retries_plain_prose(tmp_env, monkeypatch):
    """First conclusion attempt comes back as tool markup (empty content);
    the retry demands prose and gets the real answer."""
    await init_db()
    monkeypatch.setattr(settings, "max_react_iterations", 2)

    class Stubborn(_ScriptedModel):
        async def complete(self, messages, tools=None, **kw):
            last = str(messages[-1].get("content", ""))
            if "PLAIN PROSE" in last:
                yield {"type": "message", "content": "the real summary",
                       "tool_calls": [], "usage": None}
                return
            # every other call (including the first conclusion nudge):
            # tool calls with no content
            self.call += 1
            yield {"type": "message", "content": "", "tool_calls": [
                {"id": f"c{self.call}", "type": "function",
                 "function": {"name": "x", "arguments": "{}"}}], "usage": None}

    async def dispatch(name, args):
        return "ok"

    events = await _run(monkeypatch, Stubborn([]), dispatch)
    assert events[-1] == {"type": "final", "content": "the real summary"}


# --- activity persisted into GET messages -------------------------------------

@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("operator", hash_password("hunter2")),
        )
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


async def test_messages_carry_persisted_activity(client):
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', 'q1')", (cid,))
        await db.execute(
            "INSERT INTO tool_calls (conversation_id, tool, args, result) "
            "VALUES (?, 'web_search', ?, 'results here')", (cid, json.dumps({"query": "pi"})))
        await db.execute(
            "INSERT INTO tool_calls (conversation_id, tool, args, result) "
            "VALUES (?, 'web_read', '{}', 'error: nope')", (cid,))
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'assistant', 'a1')", (cid,))
        await db.commit()
    finally:
        await db.close()
    r = await client.get(f"/api/conversations/{cid}/messages")
    msgs = r.json()["messages"]
    assistant = next(m for m in msgs if m["role"] == "assistant")
    acts = assistant["activity"]
    assert [a["name"] for a in acts] == ["web_search", "web_read"]
    assert acts[0]["args"] == {"query": "pi"} and acts[0]["ok"] is True
    assert acts[1]["ok"] is False
    assert "activity" not in msgs[0]   # user message carries none


async def test_deploy_agents_tool_wired_and_guarded(client, monkeypatch):
    from backend.agent.tools import registry

    # registered + granted, and excluded from subagents (no fork bombs)
    by_name = {e["name"]: e for e in registry.compile_registry()}
    assert by_name["deploy_agents"]["requires_project"] is True
    from backend import agents_run
    await client.post("/api/agents", json={"name": "Scout"})
    specs = agents_run._agent_tools({"tools_exclude": []})
    assert all(s["function"]["name"] != "deploy_agents" for s in specs)

    # dispatch path: runs the orchestrator and returns the rollup
    import importlib.util as iu
    spec = iu.spec_from_file_location(
        "t_deploy", settings.base_dir / "tools" / "deploy_agents" / "handler.py")
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    async def fake_run_job(job_id, brief, project, *, peak=False,
                           leaf_tools=None, title=""):
        # workers never get the spawn/deploy/create/schedule tools
        names = {s["function"]["name"] for s in (leaf_tools or [])}
        assert not names & {"spawn_agent", "deploy_agents",
                            "create_agent", "schedule_update"}
        return {"root_id": 1, "rollup": f"did: {brief}", "doc_path": None}

    monkeypatch.setattr(mod, "run_job", fake_run_job)
    await client.post("/api/projects", json={"name": "Demo"})
    await client.post("/api/projects/demo/load")
    out = await mod.run("map the codebase")
    assert "did: map the codebase" in out and "Agent team finished" in out

    # recursion fence: inside a team, deploying another team is refused
    tok = mod._in_funnel.set(True)
    try:
        nested = await mod.run("another team")
    finally:
        mod._in_funnel.reset(tok)
    assert nested.startswith("error:") and "already part" in nested
