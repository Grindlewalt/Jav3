"""The agents tree read for a person (backend/agenttree.py, GET /api/chat/agents):
clean titles, roles, status and `needs`, the active/finished/all scopes, and
the background naming pass for spawned nodes.

Offline: every node is a row written straight into the test DB, plan state is a
.plan.json written through plan.save, approvals are queue rows, and the naming
pass runs against a substituted model.complete."""
import asyncio

import httpx
import pytest

from backend import agents_run, agenttree, localexec
from backend import chat as chat_mod
from backend import plan as plan_mod
from backend.agent import model as model_mod
from backend.agenttree import clean_title
from backend.auth import hash_password
from backend.db import get_db, init_db, open_conversation
from backend.main import app
from backend.memory import ensure_memory_seeds
from backend.vm import broker
from backend.vm import turn as vm_turn

SLUG = "alpha"


@pytest.fixture
async def client(tmp_env, monkeypatch):
    # ids restart at 1 in every test DB: in-flight entries another test left
    # in these module-level tables would read as this test's nodes running
    monkeypatch.setattr(chat_mod, "_active_turns", {})
    monkeypatch.setattr(agents_run, "_active_runs", {})
    monkeypatch.setattr(broker, "_envelopes", {})
    monkeypatch.setattr(vm_turn, "_live", set())
    localexec.reset_for_tests()
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
    localexec.reset_for_tests()


async def _conv(**kw) -> int:
    db = await get_db()
    try:
        return await open_conversation(db, project=kw.pop("project", None),
                                       title=kw.pop("title", "t"), **kw)
    finally:
        await db.close()


async def _sql(q, args=()):
    db = await get_db()
    try:
        cur = await db.execute(q, args)
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _say(cid, role, content, at=None):
    if at:
        await _sql("INSERT INTO messages (conversation_id, role, content, created_at) "
                   "VALUES (?, ?, ?, ?)", (cid, role, content, at))
    else:
        await _sql("INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
                   (cid, role, content))


async def _tree(client, **params):
    r = await client.get("/api/chat/agents", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --- titles -----------------------------------------------------------------------

def test_clean_title_on_the_operators_real_titles():
    assert clean_title("[gen+mesh perf] Project: /opt/jarvis/projects/benchmark-game ...") == ""
    assert agenttree._fallback(
        "[gen+mesh perf] Project: /opt/jarvis/projects/benchmark-game ...") \
        == "gen+mesh perf · benchmark-game"
    assert clean_title("[morning-stocks] Fetch stock data for tickers AAPL...") \
        == "Fetch stock data for tickers AAPL"
    assert clean_title(" Fetch today's news from NewsAPI an...") \
        == "Fetch today's news from NewsAPI an"
    assert clean_title("[item i1] Create notes/hello.txt containing exact...") \
        == "Create notes/hello.txt containing exact"
    # a lead-in names nothing: the caller moves on to its next candidate
    assert clean_title("[head] Plan: The operator's request, verbatim:") == ""
    assert clean_title("[head] Research: vector databases") == "vector databases"


def test_clean_title_drops_lead_in_lines_and_cuts_at_a_word():
    task = ("Project: /opt/jarvis/projects/benchmark-game\n\n"
            "Run the gen and mesh benchmarks, compare them against last week and "
            "write the summary to notes/perf.md")
    t = clean_title(task)
    assert t == "Run the gen and mesh benchmarks, compare them against last…"
    assert len(t) <= agenttree.TITLE_CHARS + 1
    assert clean_title("Context:\n   Build   the\tthing:") == "Build the thing"
    assert clean_title("short title") == "short title"        # no ellipsis unless cut
    assert clean_title("x" * 90).endswith("…")            # one long word still cut


async def test_titles_prefer_the_full_task_and_the_plan(client):
    # the summary is the task cut at 40 characters mid-word; the first user
    # message has all of it
    agent = await _conv(kind="agent", agent="news",
                        title="[news] Fetch today's news from NewsAPI an")
    await _say(agent, "user", "Fetch today's news from NewsAPI and file a digest")
    await _say(agent, "assistant", "done")
    named = await _conv(kind="agent", title="[x] something long")
    await _sql("UPDATE conversations SET title = 'Generated name' WHERE id = ?", (named,))
    nodes = {n["id"]: n for n in (await _tree(client, scope="all"))["nodes"]}
    assert nodes[agent]["title"] == "Fetch today's news from NewsAPI and file a digest"
    assert nodes[agent]["summary"].startswith("[news]")
    assert nodes[named]["title"] == "Generated name"


# --- roles ------------------------------------------------------------------------

def test_role_mapping():
    plans = agenttree.Plans()
    plans.heads[7] = {"items": []}
    plans.items[8] = {"id": "i2"}

    def role(**r):
        base = {"id": 1, "kind": "agent", "mode": None, "agent_slug": None, "summary": ""}
        return agenttree.role_of({**base, **r}, plans)

    assert role(kind="chat", mode="orchestrate") == "orchestrator"
    assert role(id=7, kind="head", summary="[head] something") == "plan"
    assert role(kind="head", summary="[head] Plan: old run") == "plan"
    assert role(kind="head", summary="[head] Research: x") == "research"
    assert role(kind="head", summary="[head] build it") == "leader"
    assert role(id=8, agent_slug="builder") == "item i2"
    assert role(summary="[item i5] from an older plan") == "item i5"
    assert role(agent_slug="builder") == "@builder"
    assert role(kind="chat") == "chat"
    for k in ("scout", "reader", "leader", "subagent"):
        assert role(kind=k) == k
    assert role() == "agent"


# --- status and needs -------------------------------------------------------------

async def test_plan_item_blocked_and_git_egress_approvals_need_the_operator(client):
    head = await _conv(kind="head", project=SLUG, job_id="j1",
                       title="[head] Plan: The operator's request, verbatim:")
    await _sql("UPDATE conversations SET rollup = 'synth' WHERE id = ?", (head,))
    item = await _conv(kind="agent", parent=head, project=SLUG, title="[item i1] a")
    await _say(item, "user", "brief")
    await _say(item, "assistant", "blocked")
    p = plan_mod.empty_plan(title="Ship the widget", dump="x")
    i1 = plan_mod.new_item(p, title="Write the widget")
    i1.update(status="blocked", last_error="needs an API key", conversation_id=item)
    i2 = plan_mod.new_item(p, title="Test the widget", depends_on=["i1"])
    i2.update(status="blocked", last_error="dependency i1 is blocked")
    p["items"] = [i1, i2]
    p.update(status="failed", root_id=head, job_id="j1")
    await plan_mod.save(SLUG, p)

    git_node = await _conv(kind="agent", agent="builder", project=SLUG, title="[b] commit")
    await _say(git_node, "user", "commit the fix")
    await _say(git_node, "assistant", "requested")
    await _sql("INSERT INTO git_requests (project_slug, message, conversation_id) "
               "VALUES (?, 'fix the parser', ?)", (SLUG, git_node))

    net = await _conv(kind="agent", project=SLUG, title="[t] fetch")
    await _say(net, "user", "fetch it")
    await _say(net, "assistant", "denied")
    await _sql("INSERT INTO egress_pending (project_slug, host) VALUES (?, 'api.x.com')",
               (SLUG,))
    await _sql("INSERT INTO egress_events (project_slug, conversation_id, host, verdict) "
               "VALUES (?, ?, 'api.x.com', 'deny')", (SLUG, net))
    # a denial from days ago is outside the window: that node waits on nothing
    stale = await _conv(kind="agent", project=SLUG, title="[t] old fetch")
    await _say(stale, "user", "fetch", at="2020-01-01 00:00:00")
    await _say(stale, "assistant", "denied", at="2020-01-01 00:00:01")
    await _sql("INSERT INTO egress_pending (project_slug, host) VALUES (?, 'old.x.com')",
               (SLUG,))
    await _sql("INSERT INTO egress_events (project_slug, conversation_id, host, verdict, "
               "created_at) VALUES (?, ?, 'old.x.com', 'deny', '2020-01-01 00:00:00')",
               (SLUG, stale))

    body = await _tree(client)
    nodes = {n["id"]: n for n in body["nodes"]}
    assert set(nodes) == {head, item, git_node, net}
    assert nodes[item]["status"] == "needs_you"
    assert nodes[item]["needs"] == "plan item blocked: needs an API key"
    assert nodes[item]["role"] == "item i1" and nodes[item]["title"] == "Write the widget"
    # the item that never spawned is held by the head
    assert nodes[head]["needs"] == "plan item i2 blocked: dependency i1 is blocked"
    assert nodes[head]["role"] == "plan" and nodes[head]["title"] == "Ship the widget"
    assert nodes[git_node]["needs"] == "approval pending: git commit: fix the parser"
    assert nodes[net]["needs"] == "approval pending: egress to api.x.com"
    assert nodes[net]["ended_at"] is not None and nodes[net]["running"] is False

    # the operator skips the blocked items: the plan's subtree is finished
    async with plan_mod.edit(SLUG) as p2:
        for it in p2["items"]:
            it["status"] = "skipped"
    await _sql("UPDATE git_requests SET status = 'approved'")
    nodes = {n["id"]: n for n in (await _tree(client))["nodes"]}
    assert set(nodes) == {net}
    fin = {n["id"]: n for n in (await _tree(client, scope="finished"))["nodes"]}
    assert fin[head]["status"] == "failed"              # the plan run's own status
    assert fin[item]["status"] == "done" and fin[item]["needs"] is None
    assert fin[git_node]["status"] == "done"


async def test_status_of_running_stopped_failed_and_lost_nodes(client):
    running = await _conv(kind="agent", title="[a] run")
    vm_turn._live.add(running)
    stopped = await _conv(kind="agent", title="[a] stop")
    await _say(stopped, "user", "go")
    await _say(stopped, "assistant", chat_mod.INTERRUPTED_MARKER)
    died = await _conv(kind="agent", title="[a] died")
    await _say(died, "user", "go")
    lost = await _conv(kind="head", job_id="gone", title="[head] lost job")
    broken = await _conv(kind="subagent", title="[subagent] x")
    await _sql("UPDATE conversations SET rollup = 'error: boom' WHERE id = ?", (broken,))
    local = await _conv(title="local chat", agent="builder")
    loop = asyncio.get_running_loop()
    localexec._pending[(local, "c1")] = localexec._Pending(
        local, "session", {"type": "local_tool", "id": "c1", "name": "local_shell",
                           "args": {"command": "rm -rf build"}}, loop.create_future())
    nodes = {n["id"]: n for n in (await _tree(client, scope="all"))["nodes"]}
    assert nodes[running]["status"] == "running" and nodes[running]["ended_at"] is None
    assert nodes[stopped]["status"] == "stopped"
    assert nodes[died]["status"] == "failed"
    assert nodes[lost]["status"] == "stopped"
    assert nodes[broken]["status"] == "failed"
    assert nodes[local]["status"] == "needs_you"
    assert nodes[local]["needs"] == "waiting on your answer: local_shell rm -rf build"
    active = {n["id"] for n in (await _tree(client))["nodes"]}
    assert active == {running, local}


# --- scopes -----------------------------------------------------------------------

async def test_finished_scope_pages_newest_first_with_a_total(client):
    roots = []
    for day in range(1, 6):
        r = await _conv(kind="agent", title=f"[a] job {day}")
        await _say(r, "user", f"job {day}", at=f"2026-09-0{day} 10:00:00")
        await _say(r, "assistant", "ok", at=f"2026-09-0{day} 10:05:00")
        await _sql("UPDATE conversations SET started_at = ? WHERE id = ?",
                   (f"2026-09-0{day} 09:59:00", r))
        roots.append(r)
    # a child's late activity makes its (older) root the newest finished one
    child = await _conv(kind="subagent", parent=roots[0], title="[subagent] late")
    await _say(child, "assistant", "late", at="2026-09-20 00:00:00")
    await _sql("UPDATE conversations SET started_at = '2026-09-19 00:00:00' WHERE id = ?",
               (child,))
    live = await _conv(kind="agent", title="[a] live")
    vm_turn._live.add(live)

    body = await _tree(client, scope="finished", limit=2)
    assert body["total"] == 5
    assert [n["id"] for n in body["nodes"] if n["parent_id"] is None] == [roots[0], roots[4]]
    assert [n["id"] for n in body["nodes"]][:2] == [roots[0], child]
    assert body["nodes"][0]["ended_at"] == "2026-09-20 00:00:00"
    page2 = await _tree(client, scope="finished", limit=2, offset=2)
    assert [n["id"] for n in page2["nodes"]] == [roots[3], roots[2]]
    assert page2["total"] == 5
    assert (await _tree(client, scope="finished", limit=9999))["total"] == 5  # capped, not refused
    active = await _tree(client)
    assert [n["id"] for n in active["nodes"]] == [live] and "total" not in active
    every = {n["id"] for n in (await _tree(client, scope="all"))["nodes"]}
    assert every == set(roots) | {child, live}


# --- the naming pass --------------------------------------------------------------

async def test_naming_pass_stores_title_and_leaves_summary(client, monkeypatch):
    monkeypatch.setattr(agenttree, "NAMING", True)
    asked = []

    async def fake_complete(messages, **kw):
        asked.append(messages)
        yield {"type": "message", "content": '"Daily Stock Price Digest"'}

    monkeypatch.setattr(model_mod.model, "complete", fake_complete)
    db = await get_db()
    try:
        cid, _ = await agents_run._open_run(
            db, {"name": "morning-stocks", "slug": None},
            "Fetch stock data for tickers AAPL, MSFT and NVDA and write a digest",
            active=None)
        # the plan runner titles its items itself: no call for those
        item, _ = await agents_run._open_run(
            db, {"name": "w", "slug": None}, "brief", active=None,
            title="[item i1] Write it")
    finally:
        await db.close()
    await asyncio.gather(*list(agenttree._naming_tasks))
    assert len(asked) == 1 and "AAPL" in asked[0][-1]["content"]
    db = await get_db()
    try:
        async with db.execute("SELECT id, summary, title FROM conversations "
                              "WHERE id IN (?, ?)", (cid, item)) as cur:
            rows = {r["id"]: dict(r) for r in await cur.fetchall()}
    finally:
        await db.close()
    assert rows[cid]["title"] == "Daily Stock Price Digest"
    assert rows[cid]["summary"].startswith("[morning-stocks] Fetch stock data for")
    assert rows[item]["title"] is None
    nodes = {n["id"]: n for n in (await _tree(client, scope="all"))["nodes"]}
    assert nodes[cid]["title"] == "Daily Stock Price Digest"


async def test_naming_failure_is_silent(client, monkeypatch):
    monkeypatch.setattr(agenttree, "NAMING", True)

    async def broken(messages, **kw):
        raise RuntimeError("no balance")
        yield  # pragma: no cover

    monkeypatch.setattr(model_mod.model, "complete", broken)
    cid = await _conv(kind="agent", title="[a] task")
    agenttree.name_later(cid, "do the thing")
    await asyncio.gather(*list(agenttree._naming_tasks))
    db = await get_db()
    try:
        async with db.execute("SELECT title FROM conversations WHERE id = ?", (cid,)) as cur:
            assert (await cur.fetchone())["title"] is None
    finally:
        await db.close()
