"""The explicit orchestrator (backend/plan.py): dump -> checklist -> agents that
talk. Offline throughout — the planner and the synthesis model calls are
substituted, and every item's agent turn is a scripted stand-in for
`agents_run.run_agent_turn`, so what is pinned here is the runner's contract:
the file, the edit API, dependency resolution, retry / blocked / stall
re-drive / stop, sibling messaging by item id, and the closing rollup.
"""
import asyncio
import contextlib
import json

import httpx
import pytest

from backend import agentmsg, agents_run, orchestrator, runtime
from backend import plan as plan_mod
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds
from backend.vm import broker

SLUG = "alpha"


@pytest.fixture
async def client(tmp_env, monkeypatch):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    # a run must not try to boot a guest here: the workspace hold is the
    # funnel's and is covered there
    @contextlib.asynccontextmanager
    async def no_workspace(project, *, top_level):
        yield
    monkeypatch.setattr(orchestrator, "job_workspace", no_workspace)
    monkeypatch.setattr(settings, "plan_tick_seconds", 0.01)
    monkeypatch.setattr(settings, "plan_stall_seconds", 30)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        await c.post("/api/projects", json={"name": "Alpha", "summary": "a"})
        yield c
    plan_mod._runs.clear()
    plan_mod._live_items.clear()


def _agent_file(tmp_env, slug: str) -> None:
    d = tmp_env / "agents" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "AGENT.md").write_text(f"---\nname: {slug}\ndescription: d\n---\nYou are {slug}.")


async def _put(client, items, **extra):
    r = await client.put(f"/api/projects/{SLUG}/plan", json={"items": items, **extra})
    assert r.status_code == 200, r.text
    return r.json()["plan"]


def _by_id(plan):
    return {it["id"]: it for it in plan["items"]}


async def _wait_run():
    t = plan_mod._runs.get(SLUG)
    assert t is not None, "no run was started"
    await asyncio.wait_for(t, 15)


async def _report(cid, status, summary):
    out = await plan_mod.report(SLUG, cid=cid, item_id=None, status=status, summary=summary)
    assert out.startswith("recorded"), out


def _scripted(script: dict, seen: dict):
    """A stand-in for the guest loop. `script` maps item id -> async fn(cid,
    attempt, task_text) -> final content; `seen` collects each attempt's task
    text per item so a test can assert what the brief contained."""
    async def turn(cid, system_prompt, history, **kw):
        info = plan_mod.live_item(cid)
        assert info is not None, "an item's turn started before the runner knew its conversation"
        text = history[0]["content"]
        seen.setdefault(info["item_id"], []).append(text)
        fn = script.get(info["item_id"])
        final = await fn(cid, len(seen[info["item_id"]]), text) if fn else "ok"
        yield {"type": "final", "content": final or ""}
    return turn


# --- the file and its API ----------------------------------------------------

async def test_checklist_persists_and_edits(client, tmp_env):
    _agent_file(tmp_env, "builder")
    plan = await _put(client, [{"title": "lay groundwork"},
                               {"title": "build on it", "depends_on": ["i1"],
                                "assignee": "builder"}], title="Two steps")
    assert [it["id"] for it in plan["items"]] == ["i1", "i2"]
    assert plan["title"] == "Two steps" and plan["status"] == "draft"
    path = tmp_env / "projects" / SLUG / ".plan.json"
    assert path.is_file()
    assert json.loads(path.read_text())["items"][1]["assignee"] == "builder"

    got = (await client.get(f"/api/projects/{SLUG}/plan")).json()
    assert got["running"] is False
    assert _by_id(got["plan"])["i2"]["depends_on"] == ["i1"]

    # per-item edits: title/brief, status, reorder, add, delete
    r = await client.patch(f"/api/projects/{SLUG}/plan/items/i2",
                           json={"title": "build ON it", "brief": "use the groundwork",
                                 "status": "done"})
    assert r.status_code == 200
    it = _by_id(r.json()["plan"])["i2"]
    assert it["title"] == "build ON it" and it["brief"] == "use the groundwork"
    assert it["status"] == "done"
    r = await client.post(f"/api/projects/{SLUG}/plan/items",
                          json={"title": "first, actually", "position": 0})
    assert [i["id"] for i in r.json()["plan"]["items"]] == ["i3", "i1", "i2"]
    r = await client.patch(f"/api/projects/{SLUG}/plan/items/i3", json={"position": 2})
    assert [i["id"] for i in r.json()["plan"]["items"]] == ["i1", "i2", "i3"]
    r = await client.delete(f"/api/projects/{SLUG}/plan/items/i1")
    plan = r.json()["plan"]
    assert [i["id"] for i in plan["items"]] == ["i2", "i3"]
    assert _by_id(plan)["i2"]["depends_on"] == [], "a deleted dependency must drop off"

    # what is refused
    assert (await client.patch(f"/api/projects/{SLUG}/plan/items/nope",
                               json={"title": "x"})).status_code == 404
    assert (await client.patch(f"/api/projects/{SLUG}/plan/items/i2",
                               json={"status": "running"})).status_code == 400
    assert (await client.patch(f"/api/projects/{SLUG}/plan/items/i2",
                               json={"assignee": "ghost"})).status_code == 400
    assert (await client.get("/api/projects/nope/plan")).status_code == 404
    # a PUT keeps runner-owned fields by id and honours the new order
    plan = await _put(client, [{"id": "i3", "title": "third"}, {"id": "i2", "title": "second"}])
    assert [i["id"] for i in plan["items"]] == ["i3", "i2"]
    assert _by_id(plan)["i2"]["status"] == "done"


def test_dependency_resolution_is_pure():
    plan = plan_mod.empty_plan()
    a = plan_mod.new_item(plan, title="a")
    b = plan_mod.new_item(plan, title="b", depends_on=["i1"])
    c = plan_mod.new_item(plan, title="c", depends_on=["i2"])
    d = plan_mod.new_item(plan, title="d", depends_on=["i1", "i3"])
    plan["items"] = [a, b, c, d]
    assert [i["id"] for i in plan_mod.ready(plan)] == ["i1"]
    a["status"] = "done"
    assert [i["id"] for i in plan_mod.ready(plan)] == ["i2"]
    b["status"] = "skipped"                       # skipped satisfies a dependency
    assert [i["id"] for i in plan_mod.ready(plan)] == ["i3"]
    c["status"] = "failed"
    blocked = plan_mod.propagate_blocked(plan)
    assert [i["id"] for i in blocked] == ["i4"] and d["status"] == "blocked"
    assert "i3 failed" in d["last_error"]
    assert plan_mod.finished(plan)

    # normalise: dangling and self dependencies go, cycles are broken, ids stay
    raw = {"items": [{"id": "i1", "title": "x", "depends_on": ["i2", "i1", "zz"]},
                     {"id": "i2", "title": "y", "depends_on": ["i1"]},
                     {"title": "z", "depends_on": ["i1"]}]}
    norm = plan_mod.normalise(raw)
    ids = [i["id"] for i in norm["items"]]
    assert ids == ["i1", "i2", "i3"]
    d = {i["id"]: i["depends_on"] for i in norm["items"]}
    assert "i1" not in d["i1"] and "zz" not in d["i1"], "self and dangling edges go"
    assert not ("i2" in d["i1"] and "i1" in d["i2"]), "the cycle is broken"
    assert d["i3"] == ["i1"], "an item downstream of the cycle keeps its edge"
    assert plan_mod.ready(norm), "a plan with a broken cycle must still be startable"


def test_resetting_a_failure_releases_what_it_blocked():
    plan = plan_mod.empty_plan()
    a = plan_mod.new_item(plan, title="a")
    b = plan_mod.new_item(plan, title="b", depends_on=["i1"])
    c = plan_mod.new_item(plan, title="c", depends_on=["i2"])
    s = plan_mod.new_item(plan, title="self-blocked")
    plan["items"] = [a, b, c, s]
    a["status"] = "failed"
    s["status"], s["last_error"] = "blocked", "needs the operator's API account"
    assert {i["id"] for i in plan_mod.propagate_blocked(plan)} == {"i2", "i3"}
    assert plan_mod.release_blocked(plan) == [], "nothing moves while i1 is still failed"
    a["status"] = "todo"                          # the operator resets the failure
    assert [i["id"] for i in plan_mod.release_blocked(plan)] == ["i2", "i3"]
    assert b["status"] == c["status"] == "todo" and c["last_error"] is None
    assert s["status"] == "blocked", "an item that blocked itself waits for the operator"


async def test_planner_pass_turns_a_dump_into_items(client, tmp_env, monkeypatch):
    _agent_file(tmp_env, "builder")
    (tmp_env / "projects" / SLUG / "SPEC.md").write_text("# The spec\nmake it round")
    prompts = []

    async def fake_complete(system, user, temperature=0.3):
        prompts.append(user)
        return ('Here you go:\n[{"title": "read the spec", "brief": "read SPEC.md"},'
                ' {"title": "build it", "brief": "do it", "depends_on": [0], "assignee": "builder"},'
                ' {"title": "test it", "brief": "t", "depends_on": ["i2"], "assignee": "nobody"}]')
    monkeypatch.setattr(plan_mod, "complete_text", fake_complete)

    r = await client.post(f"/api/projects/{SLUG}/plan",
                          json={"dump": "Build the thing\nround please", "files": ["SPEC.md"],
                                "confirm_peak": True})
    assert r.status_code == 200, r.text
    plan = r.json()["plan"]
    assert plan["title"] == "Build the thing"
    assert "make it round" in prompts[0] and "builder" in prompts[0]
    items = _by_id(plan)
    assert items["i2"]["depends_on"] == ["i1"] and items["i2"]["assignee"] == "builder"
    assert items["i3"]["depends_on"] == ["i2"]
    assert items["i3"]["assignee"] is None, "an assignee not in the roster is dropped"
    assert (await client.post(f"/api/projects/{SLUG}/plan", json={"dump": "  "})).status_code == 400

    # the file goes through the write chokepoint: a dump carrying a secret
    # value is refused, not persisted
    from backend import writes
    monkeypatch.setattr(writes.secrets_mod, "find_in_bytes",
                        lambda b: ["API_KEY"] if b"sk-live-123" in b else [])
    r = await client.post(f"/api/projects/{SLUG}/plan", json={"dump": "use key sk-live-123", "confirm_peak": True})
    assert r.status_code == 400 and "refused" in r.json()["detail"]
    assert _by_id(plan_mod.load(SLUG))["i1"]["title"] == "read the spec"


# --- the runner ---------------------------------------------------------------

async def test_runner_checks_items_off_retries_and_blocks(client, tmp_env, monkeypatch):
    _agent_file(tmp_env, "builder")
    await _put(client, [
        {"title": "groundwork", "brief": "dig"},
        {"title": "build", "brief": "b", "depends_on": ["i1"], "assignee": "builder"},
        {"title": "fenced", "brief": "f"},
        {"title": "doomed", "brief": "d"},
        {"title": "behind doomed", "brief": "x", "depends_on": ["i4"]},
    ], attempts_max=2, max_concurrent=2)
    seen: dict = {}

    async def i1(cid, attempt, text):
        await _report(cid, "done", "dug at code/ground.py")
        return "done"

    async def i2(cid, attempt, text):
        if attempt == 1:
            await _report(cid, "failed", "the ground was wet")
            return "failed"
        await _report(cid, "done", "built on it")
        return "ok"

    async def i3(cid, attempt, text):
        # no plan_report call: the fenced block is the accepted fallback
        return 'all good\n```json\n{"status": "done", "summary": "fenced result"}\n```'

    async def i4(cid, attempt, text):
        return "I just stopped."                    # no report at all -> failed attempt

    monkeypatch.setattr(agents_run, "run_agent_turn",
                        _scripted({"i1": i1, "i2": i2, "i3": i3, "i4": i4}, seen))

    async def fake_synth(system, user, temperature=0.3):
        assert "Run status" in user
        return "ROLLUP"
    monkeypatch.setattr(plan_mod, "complete_text", fake_synth)
    events = []
    real_publish = plan_mod.bus.publish
    monkeypatch.setattr(plan_mod.bus, "publish",
                        lambda ch, ev: (events.append(ev), real_publish(ch, ev)))

    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    job_id, root_id = r.json()["job_id"], r.json()["root_id"]
    assert r.json()["running"] is True
    assert (await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})).status_code == 409
    await _wait_run()

    plan = plan_mod.load(SLUG)
    items = _by_id(plan)
    assert plan["status"] == "failed" and plan["job_id"] == job_id and plan["root_id"] == root_id
    assert items["i1"]["status"] == "done" and items["i1"]["result_summary"] == "dug at code/ground.py"
    assert items["i2"]["status"] == "done" and items["i2"]["attempts"] == 2
    assert items["i3"]["status"] == "done" and items["i3"]["result_summary"] == "fenced result"
    assert items["i4"]["status"] == "failed" and items["i4"]["attempts"] == 2
    assert "plan_report was not called" in items["i4"]["last_error"]
    assert items["i5"]["status"] == "blocked" and items["i5"]["attempts"] == 0
    assert "i4 failed" in items["i5"]["last_error"]
    # what the briefs carried: the dependency's result, and the retry's reason
    assert "dug at code/ground.py" in seen["i2"][0]
    assert "Earlier attempts" in seen["i2"][1] and "wet" in seen["i2"][1]
    assert items["i2"]["history"][0]["outcome"] == "failed"
    assert "item:" in seen["i1"][0] and "plan_report" in seen["i1"][0]

    db = await get_db()
    try:
        async with db.execute("SELECT rollup FROM conversations WHERE id = ?", (root_id,)) as cur:
            assert (await cur.fetchone())["rollup"] == "ROLLUP"
        async with db.execute(
            "SELECT id, agent_slug, parent_conversation_id, summary FROM conversations "
            "WHERE job_id = ? AND kind = 'agent' ORDER BY id", (job_id,)) as cur:
            runs = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert len(runs) == 6, "1 + 2 (retry) + 1 + 2 (retry) attempts, all filed under the job"
    assert all(r["parent_conversation_id"] == root_id for r in runs)
    assert {r["agent_slug"] for r in runs if r["summary"].startswith("[item i2]")} == {"builder"}
    assert (tmp_env / "projects" / SLUG / "runs" / job_id / f"{root_id}-head.md").read_text() == "ROLLUP"

    kinds = [e["type"] for e in events]
    assert kinds[0] == "job_start" and kinds[-2:] == ["job_final", "job_end"]
    final = events[-2]
    assert final["plan_status"] == "failed" and final["root_id"] == root_id
    spawned = [e for e in events if e["type"] == "node_spawned" and e.get("item_id")]
    assert {e["item_id"] for e in spawned} == {"i1", "i2", "i3", "i4"}
    assert all(e["parent_id"] == root_id and e["depth"] == 1 for e in spawned)
    assert any(e["type"] == "plan_item" and e["id"] == "i5" and e["status"] == "blocked"
               for e in events)
    # the Runs tree can replay it: the item nodes are in the job's snapshot
    r = await client.get(f"/api/runs/{root_id}/tree", params={"depth": "full"})
    assert len(r.json()["nodes"]) == 7


async def test_stalled_item_is_nudged_then_respawned_once(client, monkeypatch):
    monkeypatch.setattr(settings, "plan_stall_seconds", 0.08)
    await _put(client, [{"title": "slow", "brief": "s"}])
    seen: dict = {}
    first_cid = []

    async def slow(cid, attempt, text):
        if attempt == 1:
            first_cid.append(cid)
            await asyncio.Event().wait()            # never says anything
        await _report(cid, "done", "second wind")
        return "ok"

    monkeypatch.setattr(agents_run, "run_agent_turn", _scripted({"i1": slow}, seen))
    monkeypatch.setattr(plan_mod, "complete_text", lambda *a, **k: _const("R"))
    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    it = _by_id(plan_mod.load(SLUG))["i1"]
    assert it["status"] == "done" and it["attempts"] == 2 and it["stalls"] == 1
    assert len(seen["i1"]) == 2 and "stalled" in seen["i1"][1]
    db = await get_db()
    try:
        async with db.execute(
            "SELECT from_label, body FROM agent_messages WHERE to_conversation_id = ?",
            (first_cid[0],)) as cur:
            nudges = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert len(nudges) == 1 and nudges[0]["from_label"] == "orchestrator"
    assert "[plan]" in nudges[0]["body"] and "plan_report" in nudges[0]["body"]


async def _const(v):
    return v


async def test_a_stall_names_the_hung_tool_and_respects_attempts_max(client, monkeypatch):
    """PLANS-08: the call a stalled attempt was stuck in was never recorded (the
    retry started blind), and the stall respawn ran past attempts_max."""
    monkeypatch.setattr(settings, "plan_stall_seconds", 0.05)
    monkeypatch.setattr(settings, "plan_stall_call_seconds", 0.05)    # a call is hung past this
    await _put(client, [{"title": "hangs", "brief": "h"}], attempts_max=1)

    async def turn(cid, system_prompt, history, **kw):
        yield {"type": "tool", "id": "c1", "name": "run_code", "args": {}}
        await asyncio.Event().wait()             # the call never returns
        yield {"type": "final", "content": "never"}

    monkeypatch.setattr(agents_run, "run_agent_turn", turn)
    monkeypatch.setattr(plan_mod, "complete_text", lambda *a, **k: _const("R"))
    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    it = _by_id(plan_mod.load(SLUG))["i1"]
    assert it["status"] == "failed" and it["attempts"] == 1       # not respawned past the cap
    h = it["history"][-1]
    assert h["outcome"] == "stalled" and "run_code" in h["progress"]
    assert "in flight: run_code" in it["last_error"]


async def test_siblings_talk_by_item_id_and_leave_notes(client, monkeypatch):
    await _put(client, [{"title": "left hand", "brief": "l"},
                        {"title": "right hand", "brief": "r"},
                        {"title": "later", "brief": "x", "depends_on": ["i1", "i2"]}],
               max_concurrent=2)
    seen: dict = {}
    up = {"i1": asyncio.Event(), "i2": asyncio.Event()}
    roster = []

    @contextlib.asynccontextmanager
    async def live(cid):
        env = broker.TurnEnvelope(op_id=f"t:{cid}", conversation_id=cid, active_project=SLUG)
        broker.register_turn(env)
        try:
            yield
        finally:
            broker.release_turn(env.op_id)

    async def left(cid, attempt, text):
        async with live(cid):
            up["i1"].set()
            await asyncio.wait_for(up["i2"].wait(), 5)
            db = await get_db()
            try:
                who = await agentmsg.send(db, sender_cid=cid, to="?", body="x")
                roster.append(who["error"])
                out = await agentmsg.send(db, sender_cid=cid, to="item:i2", body="hello from i1")
                assert out.get("to_cid") and not out.get("error"), out
                note = await agentmsg.send(db, sender_cid=cid, to="item:i3", body="mind the gap")
                assert note.get("note_for") == "i3", note
                gone = await agentmsg.send(db, sender_cid=cid, to="item:i9", body="?")
                assert "no item" in gone["error"]
            finally:
                await db.close()
            await _report(cid, "done", "sent")
            return "ok"

    async def right(cid, attempt, text):
        async with live(cid):
            up["i2"].set()
            db = await get_db()
            try:
                for _ in range(500):
                    rows = await agentmsg.claim(db, cid=cid, agent_slug=None)
                    if rows:
                        break
                    await asyncio.sleep(0.01)
            finally:
                await db.close()
            assert rows and rows[0]["body"] == "hello from i1"
            await _report(cid, "done", f"got: {rows[0]['body']} from {rows[0]['from_label']}")
            return "ok"

    async def later(cid, attempt, text):
        await _report(cid, "done", "later done")
        return "ok"

    monkeypatch.setattr(agents_run, "run_agent_turn",
                        _scripted({"i1": left, "i2": right, "i3": later}, seen))
    monkeypatch.setattr(plan_mod, "complete_text", lambda *a, **k: _const("R"))
    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    items = _by_id(plan_mod.load(SLUG))
    assert items["i2"]["result_summary"] == "got: hello from i1 from item i1"
    assert items["i3"]["status"] == "done"
    assert "mind the gap" in seen["i3"][0] and "from item i1" in seen["i3"][0]
    assert "item:i2 — right hand" in roster[0], roster
    assert "item:i1" not in roster[0], "the roster excludes the sender itself"


async def test_the_roster_teaches_item_addresses_for_siblings_that_are_not_live(
        client, monkeypatch):
    """The send_message discovery gap that made a plan item conclude it had
    nobody to talk to.

    A plan spawns items as their dependencies clear, so when one item asks "who
    can I reach?" its siblings are usually NOT co-live — the live-turn roster
    shows only whatever happens to be running (often just the operator's chat).
    `item:<id>` reaches them anyway (running -> delivered; todo -> a note), so
    the roster must list every sibling item's address from the plan file, not
    only the live envelopes."""
    from backend.db import open_conversation
    await _put(client, [{"title": "groundwork", "brief": "g"},
                        {"title": "build on it", "brief": "b", "depends_on": ["i1"]},
                        {"title": "and more", "brief": "m", "depends_on": ["i1"]}])
    db = await get_db()
    try:
        # only i1 is live; i2 and i3 have not started (they depend on i1) and so
        # are absent from the broker registry entirely
        cid = await open_conversation(db, project=SLUG, title="[item i1]", kind="agent")
        plan_mod._live_items[cid] = {"project": SLUG, "item_id": "i1",
                                     "title": "groundwork"}
        try:
            env = broker.TurnEnvelope(op_id=f"t:{cid}", conversation_id=cid,
                                      active_project=SLUG)
            broker.register_turn(env)
            try:
                out = await agentmsg.send(db, sender_cid=cid, to="?", body="anyone?")
            finally:
                broker.release_turn(env.op_id)
        finally:
            plan_mod._live_items.pop(cid, None)
    finally:
        await db.close()
    err = out["error"]
    assert "item:i2" in err and "item:i3" in err, (
        "a not-yet-live sibling has no address in the roster, so the model "
        f"cannot learn to reach it: {err}")
    assert "[todo]" in err, "the roster shows each item's status"
    assert "item:i1" not in err, "the sender's own item is excluded"


async def test_stop_and_operator_edits_while_running(client, monkeypatch):
    await _put(client, [{"title": "forever", "brief": "f"}, {"title": "also forever", "brief": "g"}],
               max_concurrent=2)
    seen: dict = {}

    async def forever(cid, attempt, text):
        await asyncio.Event().wait()

    monkeypatch.setattr(agents_run, "run_agent_turn",
                        _scripted({"i1": forever, "i2": forever}, seen))
    monkeypatch.setattr(plan_mod, "complete_text", lambda *a, **k: _const("R"))
    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    for _ in range(500):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(seen) == 2
    # the operator checks one off by hand: its task is cancelled, the run goes on
    r = await client.patch(f"/api/projects/{SLUG}/plan/items/i1", json={"status": "done"})
    assert r.status_code == 200 and r.json()["running"] is True
    for _ in range(500):
        if _by_id(plan_mod.load(SLUG))["i1"]["conversation_id"] not in plan_mod._live_items:
            break
        await asyncio.sleep(0.01)
    assert plan_mod.is_running(SLUG)
    # then stops the rest
    r = await client.post(f"/api/projects/{SLUG}/plan/stop")
    assert r.json()["stopped"] is True and r.json()["plan"]["items"], (
        "the panel takes every response as the plan's state")
    await _wait_run()
    plan = plan_mod.load(SLUG)
    assert plan["status"] == "stopped"
    items = _by_id(plan)
    assert items["i1"]["status"] == "done" and items["i2"]["status"] == "todo"
    assert (await client.get(f"/api/projects/{SLUG}/plan")).json()["running"] is False
    db = await get_db()
    try:
        async with db.execute("SELECT rollup FROM conversations WHERE id = ?",
                              (plan["root_id"],)) as cur:
            assert (await cur.fetchone())["rollup"].startswith("Plan run stopped")
    finally:
        await db.close()
    r = await client.post(f"/api/projects/{SLUG}/plan/stop")
    assert r.json()["stopped"] is False and r.json()["running"] is False
    assert not plan_mod._live_items


async def test_a_lost_run_is_restartable(client, monkeypatch):
    """A restart mid-run leaves items `running` in the file with no task behind
    them; Run must still start (and the runner puts them back to todo)."""
    await _put(client, [{"title": "a", "brief": "a"}])
    async with plan_mod.edit(SLUG) as plan:
        plan["status"], plan["items"][0]["status"] = "running", "running"
    seen: dict = {}

    async def ok(cid, attempt, text):
        await _report(cid, "done", "fine")
        return "ok"
    monkeypatch.setattr(agents_run, "run_agent_turn", _scripted({"i1": ok}, seen))
    monkeypatch.setattr(plan_mod, "complete_text", lambda *a, **k: _const("R"))
    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    assert _by_id(plan_mod.load(SLUG))["i1"]["status"] == "done"


async def test_plan_report_only_reports_its_own_item(client):
    await _put(client, [{"title": "a"}, {"title": "b"}])
    async with plan_mod.edit(SLUG) as plan:
        plan["items"][0]["conversation_id"] = 41
    out = await plan_mod.report(SLUG, cid=99, item_id=None, status="done", summary="s")
    assert out.startswith("error:") and "not a plan item" in out
    out = await plan_mod.report(SLUG, cid=41, item_id="i2", status="done", summary="s")
    assert out.startswith("error:") and "reports only itself" in out
    out = await plan_mod.report(SLUG, cid=41, item_id="i1", status="nope", summary="s")
    assert out.startswith("error:")
    out = await plan_mod.report(SLUG, cid=41, item_id=None, status="done", summary="did it")
    assert out.startswith("recorded")
    it = _by_id(plan_mod.load(SLUG))["i1"]
    assert it["report"]["status"] == "done" and it["result_summary"] == "did it"
    assert it["status"] == "todo", "the runner settles the status, not the report"


async def test_orchestrate_tool_plans_and_starts(client, monkeypatch):
    import importlib.util as iu
    spec = iu.spec_from_file_location(
        "t_orchestrate", settings.base_dir / "tools" / "orchestrate" / "handler.py")
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    async def fake_complete(system, user, temperature=0.3):
        return '[{"title": "one", "brief": "1"}, {"title": "two", "brief": "2", "depends_on": [0]}]'
    monkeypatch.setattr(plan_mod, "complete_text", fake_complete)
    started = []

    async def fake_start(slug):
        started.append(slug)
        return {"job_id": "j", "root_id": 7}
    monkeypatch.setattr(plan_mod, "start_run", fake_start)

    tok = runtime.active_project.set(SLUG)
    try:
        out = await mod.run(dump="do one then two", run=False)
        assert "2 items" in out and "- i2 [todo] two (after i1)" in out and not started
        out = await mod.run(dump="do one then two")
        assert started == [SLUG] and "head conversation 7" in out
        etok = runtime.ephemeral.set(True)
        try:
            out = await mod.run(dump="do it quietly")
        finally:
            runtime.ephemeral.reset(etok)
        assert out.startswith("error:") and "incognito" in out and len(started) == 1
    finally:
        runtime.active_project.reset(tok)


# --- plan_fix: the orchestrator repairs its own plan ---------------------------

async def test_fix_retries_with_guidance_and_relaunches(client, tmp_env, monkeypatch):
    await _put(client, [{"title": "root", "brief": "r"},
                        {"title": "leaf", "brief": "l", "depends_on": ["i1"]}],
               attempts_max=1, max_concurrent=2)
    seen: dict = {}

    async def i1(cid, attempt, text):
        if "use the stub" not in text:
            await _report(cid, "failed", "fixture.json missing; wrote half the loader")
            return "failed"
        await _report(cid, "done", "loader works")
        return "ok"

    async def i2(cid, attempt, text):
        await _report(cid, "done", "leaf built")
        return "ok"

    monkeypatch.setattr(agents_run, "run_agent_turn", _scripted({"i1": i1, "i2": i2}, seen))

    async def fake_synth(system, user, temperature=0.3):
        return "ROLLUP"
    monkeypatch.setattr(plan_mod, "complete_text", fake_synth)

    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    items = _by_id(plan_mod.load(SLUG))
    assert items["i1"]["status"] == "failed" and items["i2"]["status"] == "blocked"

    # a bare retry is refused: it would fail the same way
    assert (await plan_mod.fix(SLUG, action="retry", item="i1")).startswith("error:")
    out = await plan_mod.fix(SLUG, action="retry", item="i1",
                             guidance="create fixture.json yourself and use the stub")
    assert "Relaunched" in out and "i2" in out            # the blocked leaf is released
    await _wait_run()
    items = _by_id(plan_mod.load(SLUG))
    assert items["i1"]["status"] == "done" and items["i2"]["status"] == "done"
    assert items["i1"]["fixes"] == 1
    retry_text = seen["i1"][-1]
    assert "Orchestrator guidance" in retry_text and "use the stub" in retry_text
    assert "Earlier attempts" in retry_text and "half the loader" in retry_text


async def test_fix_edit_add_skip_and_cap(client, tmp_env, monkeypatch):
    await _put(client, [{"title": "a", "brief": "a"}, {"title": "b", "brief": "b"}])
    out = await plan_mod.fix(SLUG, action="add", title="stub the api", brief="write api.js",
                             depends_on=["i1"], run=False)
    assert out.startswith("added i3")
    assert (await plan_mod.fix(SLUG, action="add", title="x", depends_on=["i9"],
                               run=False)).startswith("error:")
    assert "edited" in await plan_mod.fix(SLUG, action="edit", item="i2", brief="better",
                                          run=False)
    assert "skipped" in await plan_mod.fix(SLUG, action="skip", item="i1",
                                           guidance="moot", run=False)
    items = _by_id(plan_mod.load(SLUG))
    assert items["i2"]["brief"] == "better" and items["i1"]["status"] == "skipped"
    assert items["i3"]["depends_on"] == ["i1"]
    for n in range(plan_mod.MAX_FIXES):
        await plan_mod.fix(SLUG, action="retry", item="i2", guidance=f"try {n}", run=False)
    capped = await plan_mod.fix(SLUG, action="retry", item="i2", guidance="again", run=False)
    assert capped.startswith("error:") and "Change the approach" in capped
    assert (await plan_mod.fix(SLUG, action="nope")).startswith("error:")


def test_items_get_the_plan_item_round_cap(tmp_env):
    # 12 subagent rounds went entirely on recon in the Voxelcraft run
    plan = plan_mod.empty_plan(title="t")
    it = plan_mod.new_item(plan, title="build")
    assert plan_mod._item_agent(plan, it)["max_iterations"] == settings.plan_item_max_iterations
    assert "tool rounds" in plan_mod._item_task(plan, it, [])
    plan["max_iterations"] = 7                      # an explicit plan cap still wins
    assert plan_mod._item_agent(plan, it)["max_iterations"] == 7


async def test_fix_during_the_closing_report_relaunches(client, tmp_env, monkeypatch):
    """2026-09-27: plan_fix calls landing while a finished run wrote its
    closing report were told "the live run picks it up"; nothing did, and the
    relaunch ran zero items. A fix during teardown must wait and relaunch."""
    await _put(client, [{"title": "a", "brief": "a"}], attempts_max=1)
    seen: dict = {}
    calls = {"n": 0}

    async def i1(cid, attempt, text):
        calls["n"] += 1
        if calls["n"] == 1:
            await _report(cid, "failed", "first try")
            return "failed"
        await _report(cid, "done", "second try")
        return "ok"
    monkeypatch.setattr(agents_run, "run_agent_turn", _scripted({"i1": i1}, seen))
    gate = asyncio.Event()

    async def slow_synth(system, user, temperature=0.3):
        await gate.wait()
        return "ROLLUP"
    monkeypatch.setattr(plan_mod, "complete_text", slow_synth)
    monkeypatch.setattr(plan_mod, "FIX_TEARDOWN_WAIT", 5.0)

    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    for _ in range(200):                      # the drive loop has finished...
        if SLUG in plan_mod._drive_began and SLUG not in plan_mod._driving:
            break
        await asyncio.sleep(0.01)
    assert plan_mod.is_running(SLUG)          # ...but the run is writing its report
    fix = asyncio.create_task(plan_mod.fix(SLUG, action="retry", item="i1",
                                           guidance="do it again"))
    await asyncio.sleep(0.05)
    gate.set()
    out = await fix
    assert "Relaunched" in out, out
    await _wait_run()
    assert _by_id(plan_mod.load(SLUG))["i1"]["status"] == "done"


async def test_status_is_compact_and_item_gives_detail(client, tmp_env):
    await _put(client, [{"title": f"t{n}", "brief": f"b{n}"} for n in range(22)])
    async with plan_mod.edit(SLUG) as p:
        for it in p["items"]:
            it["result_summary"] = "x" * 2000
    out = await plan_mod.status(SLUG)
    assert "i22" in out and len(out) < 12_000         # every item fits in one result
    one = await plan_mod.status(SLUG, item="i22")
    assert "# Brief" in one and "x" * 2000 in one
    assert (await plan_mod.status(SLUG, item="i99")).startswith("error:")


async def test_token_checkpoint_pauses_after_turns_finish(client, tmp_env, monkeypatch):
    """No hard budget: past the checkpoint nothing new starts, the running turn
    finishes, the run pauses, and only the operator resumes it."""
    from backend.agent import budget as budget_mod
    monkeypatch.setattr(settings, "plan_pause_tokens", 1000)
    await _put(client, [{"title": "a", "brief": "a"}, {"title": "b", "brief": "b"}],
               max_concurrent=1)
    seen: dict = {}

    async def heavy(cid, attempt, text):
        budget_mod.current().add({"prompt_tokens": 900, "completion_tokens": 200})
        await _report(cid, "done", "spent a lot")
        return "ok"
    monkeypatch.setattr(agents_run, "run_agent_turn",
                        _scripted({"i1": heavy, "i2": heavy}, seen))

    async def no_synth(system, user, temperature=0.3):
        raise AssertionError("a paused run must not call the model for a rollup")
    monkeypatch.setattr(plan_mod, "complete_text", no_synth)

    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    p = plan_mod.load(SLUG)
    items = _by_id(p)
    assert p["status"] == "paused" and p["tokens_used"] == 1100
    assert items["i1"]["status"] == "done" and items["i2"]["status"] == "todo"
    assert "i2" not in seen                                  # nothing new started
    # the orchestrator cannot relaunch it...
    out = await plan_mod.fix(SLUG, action="edit", item="i2", guidance="go")
    assert "only the operator" in out
    assert "PAUSED" in await plan_mod.status(SLUG)
    # ...the operator can, and the next checkpoint moves up
    monkeypatch.setattr(settings, "plan_pause_tokens", 10_000)
    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    p = plan_mod.load(SLUG)
    assert _by_id(p)["i2"]["status"] == "done" and p["pause_at"] == 11_100


async def test_a_plans_writes_are_pulled_home_as_items_settle(client, monkeypatch):
    """PLANS-02: an item's writes sat in the guest's shared buffer until the
    orchestrator's turn ended, so the host (git tools, panels, the operator)
    saw an empty project all run and a guest crash lost the lot. The runner now
    pulls the buffer home after each settle, attributed to the item that just
    finished, and on a timer while items run."""
    from backend.vm import guest_turn
    await _put(client, [{"title": "one", "brief": "a"},
                        {"title": "two", "brief": "b", "depends_on": ["i1"]}])
    pulled = []

    async def fake_pull(slug):
        pulled.append((slug, runtime.conversation_id.get()))
    monkeypatch.setattr(guest_turn, "pull_writes", fake_pull)
    monkeypatch.setitem(guest_turn._ws_holds, SLUG, 1)      # a guest workspace is held

    async def done(cid, attempt, text):
        await _report(cid, "done", "ok")
        return "ok"
    monkeypatch.setattr(agents_run, "run_agent_turn",
                        _scripted({"i1": done, "i2": done}, {}))

    async def fake_synth(system, user, temperature=0.3):
        return "ROLLUP"
    monkeypatch.setattr(plan_mod, "complete_text", fake_synth)
    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    items = _by_id(plan_mod.load(SLUG))
    assert all(it["status"] == "done" for it in items.values())
    cids = [c for _, c in pulled]
    # one pull per settle, each attributed to the item that had just finished
    assert items["i1"]["conversation_id"] in cids
    assert items["i2"]["conversation_id"] in cids
    # ...and none when no guest workspace is held (tests, or a guest that never came up)
    guest_turn._ws_holds.pop(SLUG, None)
    pulled.clear()
    await orchestrator.flush_workspace(SLUG, 5)
    assert pulled == []


async def test_two_simultaneous_starts_run_one_runner(client, monkeypatch):
    """ROBUST-08: start_run checked is_running, then awaited (the head
    conversation), and only then registered the task: two starts both passed the
    check and ran two runners for one plan, of which stop_run could end one."""
    await _put(client, [{"title": "a", "brief": "a"}])
    started: list[int] = []

    async def hold(cid, attempt, text):
        started.append(cid)
        await asyncio.sleep(0.2)
        await _report(cid, "done", "ok")
        return "ok"
    monkeypatch.setattr(agents_run, "run_agent_turn", _scripted({"i1": hold}, {}))
    monkeypatch.setattr(plan_mod, "complete_text", lambda *a, **k: _const("R"))
    res = await asyncio.gather(plan_mod.start_run(SLUG), plan_mod.start_run(SLUG),
                               return_exceptions=True)
    ok = [r for r in res if isinstance(r, dict)]
    bad = [r for r in res if isinstance(r, RuntimeError)]
    assert len(ok) == 1 and len(bad) == 1, res
    assert "already in progress" in str(bad[0])
    await _wait_run()
    assert len(started) == 1
    assert SLUG not in plan_mod._starting


async def test_an_item_inside_one_long_tool_call_is_not_stalled(client, monkeypatch):
    """ROBUST-17: the stall clock counted silence, and a brokered call (a
    research run, spawn_agent children, run_code) emits nothing between its
    `tool` and `tool_result`: the item was nudged, cancelled and failed while
    working. An outstanding call is activity; only a call past
    plan_stall_call_seconds counts as hung, and a call that has returned puts
    the ordinary window back."""
    monkeypatch.setattr(settings, "plan_stall_seconds", 0.1)
    monkeypatch.setattr(settings, "plan_stall_call_seconds", 5)
    await _put(client, [{"title": "long", "brief": "l"}])
    cids: list[int] = []

    async def turn(cid, system_prompt, history, **kw):
        cids.append(cid)
        yield {"type": "tool", "id": "c1", "name": "research", "args": {}}
        await asyncio.sleep(0.5)                     # 5 stall windows, one call
        yield {"type": "tool_result", "id": "c1", "name": "research", "content": "ok"}
        await _report(cid, "done", "finished")
        yield {"type": "final", "content": "ok"}

    monkeypatch.setattr(agents_run, "run_agent_turn", turn)
    monkeypatch.setattr(plan_mod, "complete_text", lambda *a, **k: _const("R"))
    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()
    it = _by_id(plan_mod.load(SLUG))["i1"]
    assert it["status"] == "done" and it["stalls"] == 0 and it["attempts"] == 1, it
    assert len(cids) == 1


async def _sender_cid() -> int:
    from backend.db import open_conversation
    db = await get_db()
    try:
        return await open_conversation(db, project=SLUG, title="sender", kind="agent")
    finally:
        await db.close()


async def test_notes_reach_blocked_and_failed_items_and_a_question_mark_keeps_the_message(
        client):
    """PLANS-12: a note to a blocked or failed item came back 'cannot reach'
    although a retry shows its brief the notes (the orchestrator re-sent the
    same text as plan_fix briefs), and `to:"?"` with a message returned the
    roster and dropped the message."""
    await _put(client, [{"title": "a", "brief": "a"}, {"title": "b", "brief": "b"},
                        {"title": "c", "brief": "c"}, {"title": "d", "brief": "d"}])
    async with plan_mod.edit(SLUG) as plan:
        st = {"i1": "blocked", "i2": "failed", "i3": "done", "i4": "todo"}
        for it in plan["items"]:
            it["status"] = st[it["id"]]
    cid = await _sender_cid()
    db = await get_db()
    try:
        for target in ("i1", "i2", "i4"):
            out = await agentmsg.send(db, sender_cid=cid, to=f"item:{target}",
                                      body=f"note for {target}")
            assert not out.get("error") and out.get("note_for") == target, out
        out = await agentmsg.send(db, sender_cid=cid, to="item:i3", body="too late")
        assert "done" in out["error"] and "outcome" in out["error"], out
        # `?` lists the addresses and holds the message; the follow-up needs only `to`
        out = await agentmsg.send(db, sender_cid=cid, to="?", body="the real message")
        assert "was not sent" in out["error"] and "Running turns" in out["error"], out
        out = await agentmsg.send(db, sender_cid=cid, to="item:i1", body="")
        assert out.get("note_for") == "i1", out
    finally:
        await db.close()
    items = _by_id(plan_mod.load(SLUG))
    assert [n["body"] for n in items["i1"]["notes"]] == ["note for i1", "the real message"]
    assert [n["body"] for n in items["i2"]["notes"]] == ["note for i2"]
    assert items["i3"]["notes"] == []
    assert "the real message" in plan_mod._item_task(plan_mod.load(SLUG), items["i1"], [])
    # an empty message with nothing held is still refused
    db = await get_db()
    try:
        out = await agentmsg.send(db, sender_cid=cid, to="item:i1", body="")
        assert "needs a message" in out["error"], out
    finally:
        await db.close()


async def test_peer_messages_inside_a_plan_run_taint_only_from_a_tainted_sender(client):
    """PLANS-11: any peer message tainted the receiver, so a plan item that was
    told something by its sibling had its journal entries quarantined as
    [unverified]. Between items of one plan run (and the head's own nudge) the
    message now carries the sender's taint instead: a sender that has read
    nothing untrusted does not taint; one that has, still does. A sender outside
    the run is a plain peer, and so is a recipient outside it."""
    from backend.agent import budget as budget_mod
    from backend.db import open_conversation
    await _put(client, [{"title": "a", "brief": "a"}, {"title": "b", "brief": "b"}])
    db = await get_db()
    try:
        i1, i2, outsider, head = [
            await open_conversation(db, project=SLUG, title=t, kind=k)
            for t, k in (("i1", "agent"), ("i2", "agent"), ("o", "chat"), ("h", "head"))]
    finally:
        await db.close()
    async with plan_mod.edit(SLUG) as plan:
        plan["root_id"] = head
    plan_mod._live_items[i1] = {"project": SLUG, "item_id": "i1", "title": "a"}
    plan_mod._live_items[i2] = {"project": SLUG, "item_id": "i2", "title": "b"}
    n = 0

    async def drained_taint(cid) -> bool:
        nonlocal n
        n += 1
        op = f"op-{cid}-{n}"
        t1, t2 = runtime.conversation_id.set(cid), budget_mod.active_op_id.set(op)
        try:
            text = await agentmsg.fetch_tool()
        finally:
            runtime.conversation_id.reset(t1)
            budget_mod.active_op_id.reset(t2)
        assert text, "nothing was delivered"
        return broker.op_tainted(op)

    async def send(frm, to, **kw):
        db = await get_db()
        try:
            out = await agentmsg.send(db, sender_cid=frm, to=to, body="hello", **kw)
        finally:
            await db.close()
        assert not out.get("error"), out

    await send(i1, "item:i2", sender_tainted=False)
    assert await drained_taint(i2) is False, "a clean sibling's message tainted the item"
    await send(i1, "item:i2", sender_tainted=True)
    assert await drained_taint(i2) is True, "a tainted sender's message must still taint"
    await send(i1, "item:i2")                          # unknown: fails closed
    assert await drained_taint(i2) is True
    await send(outsider, "item:i2", sender_tainted=False)
    assert await drained_taint(i2) is True, "a sender outside the plan run is a plain peer"
    await send(i1, str(outsider), sender_tainted=False)
    assert await drained_taint(outsider) is True, "a recipient outside the run is a plain peer"
    # the head's stall nudge is fixed plan text, not model output
    await plan_mod._nudge(head, {"id": "i2"}, {"cid": i2})
    assert await drained_taint(i2) is False, "the plan head's own nudge tainted the item"


async def test_each_item_gets_its_own_port_block_in_its_brief(client, monkeypatch):
    """PLANS-09: items on the shared box share one network namespace and each
    picked its own port (8099 held by a teammate, 8000 by an earlier item's
    server), so they collided and leaked. Every item now owns a block of ports
    (stable across its retries) and its brief says to use only those, send the
    server's output to a file and stop what it starts."""
    import re
    await _put(client, [{"title": "a", "brief": "a"}, {"title": "b", "brief": "b"},
                        {"title": "c", "brief": "c", "depends_on": ["i1"]}], max_concurrent=3)
    seen: dict = {}

    async def done(cid, attempt, text):
        await _report(cid, "failed" if (attempt == 1 and "[item i1]" in text) else "done", "x")
        return "ok"
    monkeypatch.setattr(agents_run, "run_agent_turn", _scripted(
        {"i1": done, "i2": done, "i3": done}, seen))
    monkeypatch.setattr(plan_mod, "complete_text", lambda *a, **k: _const("R"))
    r = await client.post(f"/api/projects/{SLUG}/plan/run", json={"confirm_peak": True})
    assert r.status_code == 200, r.text
    await _wait_run()

    def block(text):
        m = re.search(r"ports (\d+)–(\d+)", text)
        assert m, text[-1500:]
        return int(m.group(1)), int(m.group(2))
    b1, b2, b3 = block(seen["i1"][0]), block(seen["i2"][0]), block(seen["i3"][0])
    assert len({b1, b2, b3}) == 3, "items shared a port block"
    ranges = sorted([b1, b2, b3])
    assert all(a[1] < b[0] for a, b in zip(ranges, ranges[1:])), "port blocks overlap"
    assert len(seen["i1"]) == 2 and block(seen["i1"][1]) == b1, "a retry kept its block"
    ports_rule = seen["i2"][0].split("# Ports", 1)[1].split("\n#", 1)[0]
    assert "stop" in ports_rule and "output" in ports_rule
