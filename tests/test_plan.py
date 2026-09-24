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
    assert "Previous attempt" in seen["i2"][1] and "wet" in seen["i2"][1]
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

    async def fake_start(slug, *, peak=False):
        started.append((slug, peak))
        return {"job_id": "j", "root_id": 7}
    monkeypatch.setattr(plan_mod, "start_run", fake_start)

    tok = runtime.active_project.set(SLUG)
    try:
        out = await mod.run(dump="do one then two", run=False)
        assert "2 items" in out and "- i2 [todo] two (after i1)" in out and not started
        out = await mod.run(dump="do one then two")
        assert started == [(SLUG, True)] and "head conversation 7" in out
        etok = runtime.ephemeral.set(True)
        try:
            out = await mod.run(dump="do it quietly")
        finally:
            runtime.ephemeral.reset(etok)
        assert out.startswith("error:") and "incognito" in out and len(started) == 1
    finally:
        runtime.active_project.reset(tok)
