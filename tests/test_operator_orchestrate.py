"""Operator messages into running turns (A1), the cross-project agents tree
and node streams (A2), and orchestrator conversations (A3).

Offline throughout: chat turns substitute chat.guest_turn, agent/node turns
substitute vm.guest_turn.guest_turn (which vm/turn.py imports at call time),
and the planner substitutes plan.complete_text. The inbox drain is exercised
by calling agentmsg.fetch_tool from inside the fake turn — the same host-side
handler the guest loop's between-rounds `inbox_fetch` reaches via the broker.
"""
import asyncio
import importlib.util
import json

import httpx
import pytest

from backend import agentmsg, agents_run, bus, runtime
from backend import plan as plan_mod
from backend.agent import budget as budget_mod
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db, open_conversation
from backend.main import app
from backend.memory import ensure_memory_seeds
from backend.vm import broker
from backend.vm import turn as vm_turn

SLUG = "alpha"
MODEL = "deepseek/deepseek-flash"      # the one model enabled in the test env


@pytest.fixture
async def client(tmp_env):
    # Each test gets a fresh database, so conversation ids restart at 1. A turn
    # an earlier test left running (test_background_chat detaches them on
    # purpose) would close ITS inbox for that id when it ends — which is this
    # test's conversation too. Cancel those first, and start with no inbox open.
    from backend import chat as chat_mod
    for task in list(chat_mod._active_turns.values()):
        task.cancel()
    for task in list(chat_mod._active_turns.values()):
        try:
            await task
        except BaseException:  # noqa: BLE001 — cancelled, or failed; either way gone
            pass
    chat_mod._active_turns.clear()
    agentmsg._accepting.clear()
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        await c.post("/api/projects", json={"name": "Alpha", "summary": "a"})
        yield c
    plan_mod._runs.clear()
    vm_turn._live.clear()
    agentmsg._accepting.clear()


async def _new_turn_id(chat_mod, before=None) -> int:
    """The id of the turn just started. `before` is the set of in-flight ids
    from before the POST: an earlier test can leave an entry behind in the
    module-level table, and the largest id is not necessarily ours."""
    before = set() if before is None else before
    for _ in range(200):
        fresh = set(chat_mod._active_turns) - before
        if fresh:
            return max(fresh)
        await asyncio.sleep(0.01)
    raise AssertionError("turn never started")


async def _settle(chat_mod, cid):
    task = chat_mod._active_turns.get(cid)
    if task:
        await task


def _drain(q) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def _conv(**kw) -> int:
    db = await get_db()
    try:
        return await open_conversation(db, project=kw.pop("project", None),
                                       title=kw.pop("title", "t"), **kw)
    finally:
        await db.close()


async def _messages(client, cid):
    r = await client.get(f"/api/conversations/{cid}/messages")
    return [(m["role"], m["content"]) for m in r.json()["messages"]]


# --- A1: operator messages ------------------------------------------------------

async def test_no_turn_running_is_409(client):
    cid = await _conv()
    r = await client.post(f"/api/chat/{cid}/message", json={"text": "hello"})
    assert r.status_code == 409
    assert r.json()["detail"] == "no_turn_running"
    r = await client.post(f"/api/chat/{cid}/message", json={"text": "   "})
    assert r.status_code == 400


async def test_operator_message_reaches_the_next_round_as_the_operator(client, monkeypatch):
    from backend import chat as chat_mod
    queued, seen = asyncio.Event(), {}

    async def turn(cid, system_prompt, history, **kw):
        seen["inbox"] = kw.get("inbox")
        await queued.wait()
        note = await agentmsg.fetch_tool()     # the loop's between-rounds drain
        seen["note"] = note
        yield {"type": "inbox", "text": note}
        yield {"type": "final", "content": "done"}

    monkeypatch.setattr(chat_mod, "guest_turn", turn)
    before = set(chat_mod._active_turns)
    post = asyncio.create_task(client.post(
        "/api/chat", json={"message": "build it", "confirm_peak": True}))
    cid = await _new_turn_id(chat_mod, before)
    q = bus.subscribe(f"chat:{cid}")
    r = await client.post(f"/api/chat/{cid}/message", json={"text": "also add tests"})
    assert r.status_code == 200 and r.json() == {"queued": True}
    queued.set()
    await asyncio.wait_for(post, 5)
    await _settle(chat_mod, cid)
    events = _drain(q)
    bus.unsubscribe(f"chat:{cid}", q)

    assert seen["inbox"] is True
    # framed as the operator, never under the peer "treat as information" header
    assert agentmsg.OPERATOR_HEADER in seen["note"]
    assert "also add tests" in seen["note"]
    assert "another AGENT" not in seen["note"]
    assert {"type": "operator_message", "text": "also add tests",
            "conversation_id": cid} in events
    final = next(e for e in events if e["type"] == "final")
    assert "undelivered" not in final
    # in the transcript, verbatim and in order
    assert await _messages(client, cid) == [
        ("user", "build it"), ("user", "also add tests"), ("assistant", "done")]
    # the operator's words are not untrusted input
    assert f"chat:{cid}" not in broker._tainted


async def test_a_message_the_turn_never_reads_comes_back_as_undelivered(client, monkeypatch):
    from backend import chat as chat_mod
    queued = asyncio.Event()

    async def turn(cid, system_prompt, history, **kw):
        await queued.wait()                    # writing its final answer
        yield {"type": "final", "content": "done"}

    monkeypatch.setattr(chat_mod, "guest_turn", turn)
    before = set(chat_mod._active_turns)
    post = asyncio.create_task(client.post(
        "/api/chat", json={"message": "go", "confirm_peak": True}))
    cid = await _new_turn_id(chat_mod, before)
    tail = asyncio.create_task(client.get(f"/api/chat/{cid}/stream"))
    await asyncio.sleep(0.05)
    r = await client.post(f"/api/chat/{cid}/message", json={"text": "too late"})
    assert r.status_code == 200
    queued.set()
    body = (await asyncio.wait_for(tail, 5)).text
    await asyncio.wait_for(post, 5)
    await _settle(chat_mod, cid)
    final = next(json.loads(ln[6:]) for ln in body.splitlines()
                 if ln.startswith("data: ") and '"final"' in ln)
    assert final["undelivered"] == ["too late"]
    # handed back, not left queued too: the client re-sends it as a new turn
    assert ("user", "too late") not in await _messages(client, cid)
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) n FROM agent_messages") as cur:
            assert (await cur.fetchone())["n"] == 0
    finally:
        await db.close()
    r = await client.post(f"/api/chat/{cid}/message", json={"text": "after"})
    assert r.status_code == 409


async def test_a_closed_inbox_refuses_and_leaves_nothing_queued(client):
    cid = await _conv()
    agentmsg.open_operator_inbox(cid)
    assert await agentmsg.queue_operator_message(cid, "one")
    assert await agentmsg.close_operator_inbox(cid) == ["one"]
    assert not await agentmsg.queue_operator_message(cid, "two")
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) n FROM agent_messages") as cur:
            assert (await cur.fetchone())["n"] == 0
    finally:
        await db.close()


async def test_an_incognito_drain_takes_only_the_operators_rows(client):
    cid = await _conv(agent="builder")
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO agent_messages (from_label, to_agent_slug, body) "
            "VALUES ('scout', 'builder', 'peer mail')")
        await db.execute(
            "INSERT INTO agent_messages (from_label, to_conversation_id, body, "
            "from_operator) VALUES ('operator', ?, 'mine', 1)", (cid,))
        await db.commit()
        rows = await agentmsg.claim(db, cid=cid, agent_slug=None, operator_only=True)
        assert [r["body"] for r in rows] == ["mine"]
        async with db.execute(
            "SELECT delivered_at FROM agent_messages WHERE body = 'peer mail'") as cur:
            assert (await cur.fetchone())["delivered_at"] is None
    finally:
        await db.close()


async def test_a_spawned_node_takes_operator_messages_and_is_streamable(client, monkeypatch):
    from backend.vm import guest_turn as gt_mod
    queued = asyncio.Event()

    async def fake_guest(cid, system_prompt, history, **kw):
        await queued.wait()
        yield {"type": "token", "text": "working"}
        yield {"type": "final", "content": "node done"}

    monkeypatch.setattr(gt_mod, "guest_turn", fake_guest)
    cid = await _conv(kind="agent", title="[scout] x")

    async def consume():
        return [ev async for ev in vm_turn.run_agent_turn(
            cid, "sys", [{"role": "user", "content": "x"}], tools=[], read_only=[])]

    run = asyncio.create_task(consume())
    for _ in range(100):
        if cid in vm_turn.live_nodes():
            break
        await asyncio.sleep(0.01)
    tail = asyncio.create_task(client.get(f"/api/chat/agents/{cid}/stream"))
    await asyncio.sleep(0.05)
    r = await client.post(f"/api/chat/{cid}/message", json={"text": "stop early"})
    assert r.status_code == 200
    queued.set()
    events = await asyncio.wait_for(run, 5)
    body = (await asyncio.wait_for(tail, 5)).text
    assert events[-1]["undelivered"] == ["stop early"]
    assert '"working"' in body and '"node done"' in body and "stop early" in body
    assert cid not in vm_turn.live_nodes()
    r = await client.post(f"/api/chat/{cid}/message", json={"text": "again"})
    assert r.status_code == 409
    # finished: both stream URLs answer idle
    assert '"idle"' in (await client.get(f"/api/chat/{cid}/stream")).text


async def test_a_running_head_streams_its_job_and_ends_with_the_rollup(client):
    head = await _conv(kind="head", job_id="job1", title="[head] Plan: x")
    budget_mod.register("job1", budget_mod.Budget(1000, 1000))
    try:
        tail = asyncio.create_task(client.get(f"/api/chat/{head}/stream"))
        await asyncio.sleep(0.05)
        bus.publish("job1", {"type": "node_status", "node_id": head, "status": "running"})
        bus.publish("job1", {"type": "job_final", "rollup": "all done"})
        body = (await asyncio.wait_for(tail, 5)).text
    finally:
        budget_mod.release("job1")
    assert '"node_status"' in body
    assert '"final"' in body and "all done" in body
    # no budget any more: a head without a rollup is a lost job, not a live one
    assert '"idle"' in (await client.get(f"/api/chat/{head}/stream")).text


# --- A2: the agents tree ----------------------------------------------------------

async def test_agents_tree_nests_every_kind_and_marks_running(client, monkeypatch):
    # conversation ids restart at 1 in every test DB, so an in-flight entry
    # another test left behind would read as one of these running
    from backend import chat as chat_mod
    monkeypatch.setattr(chat_mod, "_active_turns", {})
    monkeypatch.setattr(agents_run, "_active_runs", {})
    monkeypatch.setattr(broker, "_envelopes", {})
    orch =await _conv(title="orch", mode="orchestrate", project=SLUG, locked=True)
    head = await _conv(kind="head", parent=orch, job_id="jobA", title="[head] Plan: p")
    item = await _conv(kind="agent", parent=head, title="[item i1] a", model=MODEL)
    chat = await _conv(title="plain chat that spawned")
    spawned = await _conv(kind="agent", parent=chat, title="[scout] look")
    lonely = await _conv(title="nothing spawned")
    voice_next = await _conv(title="voice continuation", parent=chat)
    thread = await _conv(title="builder thread", agent="builder")
    vm_turn._live.add(item)
    budget_mod.register("jobA", budget_mod.Budget(1000, 1000))
    try:
        # scope=all: the historic view (the default is now active roots only)
        r = await client.get("/api/chat/agents", params={"scope": "all"})
    finally:
        budget_mod.release("jobA")
        vm_turn._live.discard(item)
    assert r.status_code == 200
    nodes = {n["id"]: n for n in r.json()["nodes"]}
    assert lonely not in nodes and voice_next not in nodes
    assert nodes[orch]["kind"] == "orchestrator" and nodes[orch]["parent_id"] is None
    assert nodes[orch]["project"] == SLUG
    assert nodes[head]["parent_id"] == orch and nodes[head]["kind"] == "head"
    assert nodes[head]["running"] is True
    assert nodes[item]["parent_id"] == head and nodes[item]["running"] is True
    assert nodes[item]["model"] == MODEL
    assert nodes[chat]["kind"] == "chat" and nodes[spawned]["parent_id"] == chat
    assert nodes[thread]["agent_slug"] == "builder"
    assert nodes[orch]["running"] is False
    order = [n["id"] for n in r.json()["nodes"]]
    assert order.index(orch) < order.index(head) < order.index(item)
    # every field older clients read is still there, plus agenttree's
    assert set(nodes[orch]) == {"id", "parent_id", "kind", "title", "agent_slug",
                                "project", "model", "running", "started_at",
                                "summary", "role", "status", "needs", "ended_at"}


async def test_node_transcripts_open_through_the_chat_router(client):
    item = await _conv(kind="agent", title="[item i1] a")
    db = await get_db()
    try:
        await db.execute("INSERT INTO messages (conversation_id, role, content) "
                         "VALUES (?, 'user', 'brief')", (item,))
        await db.commit()
    finally:
        await db.close()
    r = await client.get(f"/api/conversations/{item}/messages")
    assert r.status_code == 200
    assert r.json()["messages"][0]["content"] == "brief"
    assert r.json()["running"] is False


# --- A3: orchestration -------------------------------------------------------------

async def test_orchestrate_mode_needs_a_project_and_a_persistent_chat(client):
    r = await client.post("/api/chat", json={"message": "x", "mode": "orchestrate",
                                             "confirm_peak": True})
    assert r.status_code == 400
    r = await client.post("/api/chat", json={"message": "x", "mode": "orchestrate",
                                             "project": SLUG, "ephemeral": True,
                                             "confirm_peak": True})
    assert r.status_code == 400
    r = await client.post("/api/chat", json={"message": "x", "mode": "orchestrate",
                                             "project": "nope", "confirm_peak": True})
    assert r.status_code == 404


async def test_an_orchestrator_turn_gets_its_prompt_tools_and_cap(client, monkeypatch):
    from backend import chat as chat_mod
    seen = {}

    async def turn(cid, system_prompt, history, **kw):
        seen.update(prompt=system_prompt, kw=kw)
        yield {"type": "final", "content": "planned"}

    monkeypatch.setattr(chat_mod, "guest_turn", turn)
    r = await client.post("/api/chat", json={
        "message": "dump: build A, B and C", "mode": "orchestrate",
        "project": SLUG, "confirm_peak": True})
    assert r.status_code == 200 and '"planned"' in r.text
    cid = max(c["id"] for c in (await client.get("/api/conversations")).json()["conversations"])
    await _settle(chat_mod, cid)
    assert "orchestrator" in seen["prompt"] and f"project {SLUG}" in seen["prompt"]
    names = {t["function"]["name"] for t in seen["kw"]["tool_specs"]}
    assert {"orchestrate", "plan_status", "send_message", "spawn_agent"} <= names
    assert seen["kw"]["max_iterations"] == settings.orchestrator_max_iterations
    db = await get_db()
    try:
        async with db.execute("SELECT mode, kind FROM conversations WHERE id = ?",
                              (cid,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    assert (row["mode"], row["kind"]) == ("orchestrate", "chat")
    nodes = (await client.get("/api/chat/agents", params={"scope": "all"})).json()["nodes"]
    assert any(n["id"] == cid and n["kind"] == "orchestrator" for n in nodes)
    # an ordinary chat is offered no plan_status
    seen.clear()
    await client.post("/api/chat", json={"message": "hi", "project": SLUG,
                                         "confirm_peak": True})
    names = {t["function"]["name"] for t in seen["kw"]["tool_specs"]}
    assert "plan_status" not in names and "orchestrator" not in seen["prompt"]
    await asyncio.sleep(0.05)


def _handler(name):
    spec = importlib.util.spec_from_file_location(
        f"h_{name}", settings.base_dir / "tools" / name / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_spawn_tools_take_an_explicit_model_and_refuse_a_bad_one(client, monkeypatch):
    seen = {}

    async def fake_headless(slug, task, active=None, *, model=None, **kw):
        seen["model"] = model
        return {"conversation_id": 999, "agent": slug, "final": "ok"}

    monkeypatch.setattr(agents_run, "run_agent_headless", fake_headless)
    spawn = _handler("spawn_agent")
    out = await spawn.run(agent="scout", task="t", model="nope/nothing")
    assert out.startswith("error:") and "model" not in seen
    out = await spawn.run(agent="scout", task="t", model=MODEL)
    assert "ok" in out and seen["model"] == MODEL
    await spawn.run(agent="scout", task="t")
    assert seen["model"] is None

    async def fake_temp(prompt, task, **kw):
        seen["temp"] = kw.get("model")
        return {"conversation_id": 999, "agent": "temp", "final": "ok"}

    monkeypatch.setattr(agents_run, "run_temp_agent_headless", fake_temp)
    temp = _handler("spawn_temp_agent")
    assert (await temp.run(task="t", prompt="p", model="bad/model")).startswith("error:")
    await temp.run(task="t", prompt="p", model=MODEL)
    assert seen["temp"] == MODEL


async def test_the_planner_can_only_assign_models_the_operator_named(client, monkeypatch):
    seen = {}

    async def fake_complete(system, user, *a, **k):
        seen.update(system=system, user=user)
        return json.dumps([
            {"title": "a", "brief": "a", "model": MODEL},
            {"title": "b", "brief": "b", "model": "openai/gpt-9"},
            {"title": "c", "brief": "c"}])

    monkeypatch.setattr(plan_mod, "complete_text", fake_complete)
    with pytest.raises(ValueError):
        plan_mod.checked_models([{"task": "a", "model": "bad/model"}])
    assigned = plan_mod.checked_models([{"task": "the a part", "model": MODEL}])
    plan = await plan_mod.plan_from_dump(SLUG, "do a b c", models=assigned)
    assert [it["model"] for it in plan["items"]] == [MODEL, None, None]
    assert "# Model assignments" in seen["user"] and "the a part" in seen["user"]
    assert "(model " + MODEL + ")" in plan_mod.render_checklist(plan)
    # no assignments: whatever the planner says, nothing gets a model
    plan = await plan_mod.plan_from_dump(SLUG, "do a b c")
    assert [it["model"] for it in plan["items"]] == [None, None, None]
    assert "Model assignments" not in seen["system"]
    # the item's agent runs on it
    plan["items"][0]["model"] = MODEL
    assert plan_mod._item_agent(plan, plan["items"][0])["model"] == MODEL


async def test_orchestrate_refuses_an_unrunnable_model_before_planning(client, monkeypatch):
    called = []
    monkeypatch.setattr(plan_mod, "complete_text", lambda *a, **k: called.append(1))
    tok = runtime.active_project.set(SLUG)
    try:
        out = await _handler("orchestrate").run(
            dump="x", models=[{"task": "x", "model": "bad/model"}])
    finally:
        runtime.active_project.reset(tok)
    assert out.startswith("error:") and not called


async def test_plan_status_waits_and_returns_early_for_a_message(client, monkeypatch):
    monkeypatch.setattr(plan_mod, "STATUS_POLL_SECONDS", 0.01)
    assert (await plan_mod.status(SLUG)).startswith("error:")
    plan = plan_mod.empty_plan(title="P")
    plan["items"] = [plan_mod.new_item(plan, title="first")]
    async with plan_mod._lock(SLUG):
        await plan_mod.save(SLUG, plan)
    forever = asyncio.create_task(asyncio.sleep(3600))
    plan_mod._runs[SLUG] = forever
    orch = await _conv(mode="orchestrate")
    try:
        agentmsg.open_operator_inbox(orch)
        assert await agentmsg.queue_operator_message(orch, "change of plan")
        out = await asyncio.wait_for(plan_mod.status(SLUG, wait_seconds=60, cid=orch), 5)
        assert "A message arrived" in out and "RUNNING" in out and "first" in out
        await agentmsg.close_operator_inbox(orch)

        async def finish():
            await asyncio.sleep(0.05)
            forever.cancel()
        asyncio.create_task(finish())
        out = await asyncio.wait_for(plan_mod.status(SLUG, wait_seconds=60, cid=orch), 5)
        assert "The run finished" in out
    finally:
        forever.cancel()
