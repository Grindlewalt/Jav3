"""General agents: a chat thread that runs AS an agent.

The operator's ask was "agents that live in a project and help me build
things, like a Claude Code session". That is the chat path plus an identity —
NOT a second runtime — so these tests pin the seam rather than the machinery:
a conversation carries `agent_slug`, the turn assembles the AGENT.md prompt
ahead of the shared context, the definition's exclusions trim the tools the
project already allowed, and the identity does not drift across turns.

Offline: `chat.guest_turn` and `agents_run.run_agent_turn` are substituted, so
nothing here needs an API key.

Also here: the run-tree seams the Agent Outputs view and inter-agent messaging
build on — definition `project` binding precedence, `parent`/`agent_slug`
stamping, delete safety for the new parent links, the outputs query, and
own_memory's private notes dir.
"""
import asyncio
import contextlib

import httpx
import pytest

from backend.auth import hash_password
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
        for name in ("Alpha", "Beta"):
            await c.post("/api/projects", json={"name": name, "summary": name})
        yield c


def _capturing_turn(seen: list[dict]):
    """Stand-in for the guest loop that records the turn spec it was handed."""
    async def turn(cid, system_prompt, history, **kw):
        seen.append({"system_prompt": system_prompt, "history": history, **kw})
        yield {"type": "final", "content": "ok"}
    return turn


async def _settle():
    """Wait for the detached turn task to finish unwinding.

    POST /api/chat returns as soon as its SSE tail sees `final`, but the turn's
    `finally` — which closes the turn's DB handle — runs after that. Ending a
    test in that window strands an aiosqlite worker thread on a closed event
    loop, and a stranded non-daemon thread hangs the interpreter at exit. The
    handle is closed before the conversation leaves `_active_turns`, so
    draining that dict is the deterministic wait.
    """
    from backend import chat as chat_mod
    for _ in range(600):
        if not chat_mod._active_turns:
            return
        await asyncio.sleep(0.005)     # the close hops a worker thread: real time
    raise AssertionError("a chat turn never finished")


async def _make_agent(client, **fields):
    await client.post("/api/agents", json={"name": "Builder"})
    a = (await client.get("/api/agents/builder")).json()
    a.update({"prompt": "You are Builder. You build things.", **fields})
    assert (await client.put("/api/agents/builder", json=a)).status_code == 200
    return a


async def test_chat_thread_runs_as_the_agent(client, monkeypatch):
    """The whole feature in one turn: identity is stored on the conversation,
    the AGENT.md prompt leads the sandwich, and the definition's exclusions
    take tools away — including via skills_exclude, which bit nothing before
    (skills compile into the same registry as tools)."""
    from backend import chat as chat_mod
    await _make_agent(client, tools_exclude=["web_search"],
                      skills_exclude=["web_read"], max_iterations=9)
    seen: list[dict] = []
    monkeypatch.setattr(chat_mod, "guest_turn", _capturing_turn(seen))

    r = await client.post("/api/chat", json={"message": "hello",
                                             "confirm_peak": True,
                                             "agent": "builder"})
    assert r.status_code == 200
    await _settle()
    assert len(seen) == 1
    spec = seen[0]

    # the agent's prompt leads, the shared context follows
    assert spec["system_prompt"].startswith("You are Builder. You build things.")
    # ...and the operator's rules tail is still there: an agent definition
    # cannot opt out of them
    assert "operator" in spec["system_prompt"].lower()

    names = {t["function"]["name"] for t in spec["tool_specs"]}
    assert "web_search" not in names          # tools_exclude
    assert "web_read" not in names            # skills_exclude, same namespace
    assert "read_file" in names               # everything else still granted
    assert spec["max_iterations"] == 9

    db = await get_db()
    try:
        async with db.execute(
            "SELECT agent_slug, kind FROM conversations") as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    assert row["agent_slug"] == "builder"
    # deliberately an ordinary chat: it belongs in the chat sidebar, gets
    # compaction, and is stoppable/resumable through the chat endpoints
    assert row["kind"] == "chat"
    listed = (await client.get("/api/conversations")).json()["conversations"]
    assert [c["agent_slug"] for c in listed] == ["builder"]


async def test_plain_chat_is_untouched(client, monkeypatch):
    """No agent = central Jarvis, exactly as before."""
    from backend import chat as chat_mod
    await _make_agent(client, tools_exclude=["web_search"])
    seen: list[dict] = []
    monkeypatch.setattr(chat_mod, "guest_turn", _capturing_turn(seen))

    r = await client.post("/api/chat", json={"message": "hi", "confirm_peak": True})
    assert r.status_code == 200
    await _settle()
    assert not seen[0]["system_prompt"].startswith("You are Builder")
    assert seen[0]["max_iterations"] is None
    names = {t["function"]["name"] for t in seen[0]["tool_specs"]}
    assert "web_search" in names


async def test_identity_is_pinned_and_multi_turn(client, monkeypatch):
    """A thread's identity binds at creation and cannot be swapped underneath
    the transcript — and the follow-up is a real second turn, with the first
    exchange in its history."""
    from backend import chat as chat_mod
    await _make_agent(client)
    seen: list[dict] = []
    monkeypatch.setattr(chat_mod, "guest_turn", _capturing_turn(seen))

    r = await client.post("/api/chat", json={"message": "first",
                                             "confirm_peak": True,
                                             "agent": "builder"})
    assert r.status_code == 200
    await _settle()
    cid = None
    db = await get_db()
    try:
        async with db.execute("SELECT id FROM conversations") as cur:
            cid = (await cur.fetchone())["id"]
    finally:
        await db.close()

    # a second message naming a DIFFERENT agent must not re-cast the thread
    await client.post("/api/agents", json={"name": "Other"})
    r = await client.post("/api/chat", json={"message": "second",
                                             "conversation_id": cid,
                                             "confirm_peak": True,
                                             "agent": "other"})
    assert r.status_code == 200
    await _settle()
    assert len(seen) == 2
    assert seen[1]["system_prompt"].startswith("You are Builder")
    # multi-turn: the earlier exchange rides along (this is what the one-shot
    # agent run path never had)
    assert any(m.get("content") == "first" for m in seen[1]["history"])


async def test_unknown_agent_is_a_404_not_a_dead_thread(client):
    r = await client.post("/api/chat", json={"message": "hi", "confirm_peak": True,
                                             "agent": "nope"})
    assert r.status_code == 404
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) AS c FROM conversations") as cur:
            assert (await cur.fetchone())["c"] == 0   # no orphan row
    finally:
        await db.close()


async def test_agent_run_stop(client, monkeypatch):
    """Agents never had a stop endpoint even though _active_runs held the task
    handle. Cancelling must also leave the interruption in the transcript, the
    way a stopped chat turn does."""
    from backend import agents_run, chat as chat_mod
    await _make_agent(client)
    release = asyncio.Event()
    started = asyncio.Event()

    async def blocking_turn(cid, system_prompt, history, **kw):
        started.set()
        await release.wait()
        yield {"type": "final", "content": "never"}

    monkeypatch.setattr(agents_run, "run_agent_turn", blocking_turn)
    post = asyncio.create_task(client.post(
        "/api/agents/builder/run", json={"task": "go", "confirm_peak": True}))
    await asyncio.wait_for(started.wait(), 5)
    cid = max(agents_run._active_runs)
    task = agents_run._active_runs[cid]

    r = await client.post(f"/api/agents/runs/{cid}/stop")
    assert r.json() == {"stopped": True}
    with contextlib.suppress(asyncio.CancelledError):
        await task            # let the run unwind before reading its transcript
    assert cid not in agents_run._active_runs
    assert (await client.post(f"/api/agents/runs/{cid}/stop")).json() == {"stopped": False}

    msgs = (await client.get(f"/api/conversations/{cid}/messages")).json()["messages"]
    assert msgs[-1]["content"] == chat_mod.INTERRUPTED_MARKER

    release.set()
    post.cancel()
    with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
        await post


async def test_interactive_run_honours_max_iterations(client, monkeypatch):
    """max_iterations was read on the headless path and ignored here, so one
    definition ran two different caps depending on who started it."""
    from backend import agents_run
    await _make_agent(client, max_iterations=5)
    seen: list[dict] = []
    release, started = asyncio.Event(), asyncio.Event()

    async def turn(cid, system_prompt, history, **kw):
        seen.append(kw)
        started.set()
        await release.wait()
        yield {"type": "final", "content": "done"}

    monkeypatch.setattr(agents_run, "run_agent_turn", turn)
    post = asyncio.create_task(client.post(
        "/api/agents/builder/run", json={"task": "go", "confirm_peak": True}))
    await asyncio.wait_for(started.wait(), 5)
    task = agents_run._active_runs[max(agents_run._active_runs)]
    release.set()
    await task                 # deterministic: the run closes its DB handle here
    assert (await post).status_code == 200
    assert seen[0]["max_iterations"] == 5


# --- run tree: identity, parent links, project binding ----------------------

async def _row(cid: int) -> dict:
    db = await get_db()
    try:
        async with db.execute(
            "SELECT c.*, p.slug AS project FROM conversations c "
            "LEFT JOIN projects p ON p.id = c.project_id WHERE c.id = ?",
            (cid,)) as cur:
            r = await cur.fetchone()
    finally:
        await db.close()
    return dict(r) if r else None


async def _open_chat(**kw) -> int:
    from backend.db import open_conversation
    db = await get_db()
    try:
        return await open_conversation(db, project=None, title="parent chat", **kw)
    finally:
        await db.close()


async def _run_interactive_to_end(client, monkeypatch, body: dict) -> int:
    """Start an interactive run, let it finish, return its conversation id."""
    from backend import agents_run

    async def turn(cid, system_prompt, history, **kw):
        yield {"type": "final", "content": "done"}

    monkeypatch.setattr(agents_run, "run_agent_turn", turn)
    r = await client.post("/api/agents/builder/run",
                          json={"task": "go", "confirm_peak": True, **body})
    assert r.status_code == 200, r.text
    for _ in range(600):
        if not agents_run._active_runs:
            break
        await asyncio.sleep(0.005)
    db = await get_db()
    try:
        async with db.execute("SELECT MAX(id) AS m FROM conversations") as cur:
            return (await cur.fetchone())["m"]
    finally:
        await db.close()


async def test_definition_project_is_validated_on_save(client):
    a = await _make_agent(client)
    r = await client.put("/api/agents/builder", json={**a, "project": "nope"})
    assert r.status_code == 400
    r = await client.put("/api/agents/builder", json={**a, "project": "beta"})
    assert r.status_code == 200
    assert (await client.get("/api/agents/builder")).json()["project"] == "beta"
    listed = (await client.get("/api/agents")).json()["agents"]
    assert listed[0]["project"] == "beta"


async def test_interactive_run_binding_and_identity(client, monkeypatch):
    """request `project` > definition `project` > global active, and every
    interactive run carries its agent_slug (addressable by name)."""
    await _make_agent(client, project="beta")
    await client.post("/api/projects/alpha/load")      # global active = alpha
    cid = await _run_interactive_to_end(client, monkeypatch, {})
    row = await _row(cid)
    assert row["project"] == "beta"                     # definition beats global
    assert row["agent_slug"] == "builder" and row["kind"] == "agent"

    cid = await _run_interactive_to_end(client, monkeypatch, {"project": "alpha"})
    assert (await _row(cid))["project"] == "alpha"      # request beats definition


async def test_headless_binding_precedence(client):
    from fastapi import HTTPException

    from backend import runtime
    from backend.agents_api import _read
    from backend.agents_run import _USE_DB, _open_run
    await _make_agent(client)
    tok = runtime.active_project.set("alpha")           # the caller's pin
    db = await get_db()
    try:
        _, active = await _open_run(db, _read("builder"), "t", active=_USE_DB)
        assert active == "alpha"                        # no definition project
        a = (await client.get("/api/agents/builder")).json()
        await client.put("/api/agents/builder", json={**a, "project": "beta"})
        _, active = await _open_run(db, _read("builder"), "t", active=_USE_DB)
        assert active == "beta"                         # definition beats the pin
        _, active = await _open_run(db, _read("builder"), "t", active="alpha")
        assert active == "alpha"                        # explicit request wins
        _, active = await _open_run(db, _read("builder"), "t", active=None)
        assert active == "beta"          # a schedule with no project: definition

        # a bound project deleted after the save is a loud 404, not a silent
        # run somewhere else
        await db.execute("UPDATE projects SET deleted_at = datetime('now') "
                         "WHERE slug = 'beta'")
        await db.commit()
        with pytest.raises(HTTPException) as ei:
            await _open_run(db, _read("builder"), "t", active=_USE_DB)
        assert ei.value.status_code == 404 and "beta" in ei.value.detail
    finally:
        await db.close()
        runtime.active_project.reset(tok)


async def test_chat_thread_opens_in_the_definition_project(client, monkeypatch):
    from backend import chat as chat_mod
    await _make_agent(client, project="beta")
    await client.post("/api/projects/alpha/load")
    seen: list[dict] = []
    monkeypatch.setattr(chat_mod, "guest_turn", _capturing_turn(seen))

    await client.post("/api/chat", json={"message": "a", "confirm_peak": True,
                                         "agent": "builder"})
    await _settle()
    assert seen[-1]["active_slug"] == "beta"
    # an explicit request still wins — here, deliberately no project
    await client.post("/api/chat", json={"message": "b", "confirm_peak": True,
                                         "agent": "builder", "project_mode": "none"})
    await _settle()
    assert seen[-1]["active_slug"] is None


async def test_spawned_children_get_parent_and_identity(client, monkeypatch):
    """spawn_agent / spawn_temp_agent children used to open with parent=None,
    disconnecting the tree exactly where Jarvis delegates. A named child runs
    AS its definition (agent_slug); a temp child has no definition, so none —
    it is reachable from its parent instead."""
    from backend import agents_run, runtime

    async def turn(cid, system_prompt, history, **kw):
        yield {"type": "final", "content": "report"}

    monkeypatch.setattr(agents_run, "run_agent_turn", turn)
    await _make_agent(client)
    chat_id = await _open_chat()
    tok = runtime.conversation_id.set(chat_id)
    try:
        named = await agents_run.run_agent_headless("builder", "go")
        temp = await agents_run.run_temp_agent_headless("You check.", "check")
    finally:
        runtime.conversation_id.reset(tok)
    n, t = await _row(named["conversation_id"]), await _row(temp["conversation_id"])
    assert n["parent_conversation_id"] == chat_id and n["agent_slug"] == "builder"
    assert t["parent_conversation_id"] == chat_id and t["agent_slug"] is None

    # deleting the parent chat must not trip the new foreign keys
    r = await client.delete(f"/api/conversations/{chat_id}")
    assert r.status_code == 200
    assert (await _row(named["conversation_id"]))["parent_conversation_id"] is None
    assert await _row(chat_id) is None


async def test_incognito_wipe_survives_a_spawned_child(client, monkeypatch):
    """An incognito turn that spawned an agent leaves a child row pointing at
    it. The wipe in _run_chat_turn's finally must clear that link first, or it
    raises there and bricks the conversation (never leaves _active_turns)."""
    from backend import chat as chat_mod
    from backend.db import open_conversation
    child: dict = {}

    async def turn(cid, system_prompt, history, **kw):
        db = await get_db()
        try:
            child["id"] = await open_conversation(
                db, project=None, title="[spawned]", kind="agent", parent=cid)
        finally:
            await db.close()
        child["parent"] = cid
        yield {"type": "final", "content": "ok"}

    monkeypatch.setattr(chat_mod, "guest_turn", turn)
    r = await client.post("/api/chat", json={"message": "hi", "ephemeral": True,
                                             "confirm_peak": True})
    assert r.status_code == 200
    await _settle()
    assert await _row(child["parent"]) is None          # wiped
    assert (await _row(child["id"]))["parent_conversation_id"] is None


async def test_job_head_links_to_the_launching_turn(client, monkeypatch):
    """The chat-to-job link used to be a live-only bus event; the head now
    records its launcher as parent, so a reloaded chat re-finds its jobs, and
    the job's events say which agent it works for."""
    from backend import bus, orchestrator, runtime
    await _make_agent(client)
    thread = await _open_chat(agent="builder")

    async def fake_node(**kw):
        return {"cid": kw["cid"], "kind": "head", "output": "x", "rollup": "done"}

    monkeypatch.setattr(orchestrator, "run_node", fake_node)
    q = bus.subscribe("job-1")
    tok = runtime.conversation_id.set(thread)
    try:
        res = await orchestrator.run_job("job-1", "brief", "")
    finally:
        runtime.conversation_id.reset(tok)
        bus.unsubscribe("job-1", q)
    head = await _row(res["root_id"])
    assert head["parent_conversation_id"] == thread
    assert head["agent_slug"] is None        # the funnel is not builder itself
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    start = next(e for e in events if e.get("type") == "job_start")
    assert start["agent_slug"] == "builder"

    db = await get_db()
    try:
        await db.execute("UPDATE conversations SET rollup = NULL WHERE id = ?",
                         (res["root_id"],))
        await db.commit()
    finally:
        await db.close()
    jobs = (await client.get(f"/api/conversations/{thread}/messages")).json()["jobs"]
    assert [(j["root_id"], j["job_id"], j["running"]) for j in jobs] == [
        (res["root_id"], "job-1", True)]


async def test_outputs_endpoint(client, tmp_env):
    """Everything that ran AS the agent, plus everything descended from it,
    newest first, with the fields the Outputs tab renders."""
    from backend.db import open_conversation
    await _make_agent(client)
    thread = await _open_chat(agent="builder")
    other = await _open_chat()                            # unrelated chat
    db = await get_db()
    try:
        temp = await open_conversation(db, project="alpha", title="[temp] x",
                                       kind="agent", parent=thread)
        head = await open_conversation(db, project="alpha", title="[head] y",
                                       kind="head", parent=temp, job_id="j9")
        await db.execute("UPDATE conversations SET rollup = 'all done' WHERE id = ?",
                         (head,))
        await db.execute("INSERT INTO messages (conversation_id, role, content) "
                         "VALUES (?, 'assistant', ?)", (thread, "the  final\nanswer"))
        await db.commit()
    finally:
        await db.close()
    runs = tmp_env / "projects" / "alpha" / "runs" / "j9"
    runs.mkdir(parents=True)
    (runs / f"{head}-head.md").write_text("rollup")

    body = (await client.get("/api/agents/builder/outputs")).json()
    assert body["slug"] == "builder"
    by_id = {o["id"]: o for o in body["outputs"]}
    assert set(by_id) == {thread, temp, head} and other not in by_id
    assert by_id[thread]["snippet"] == "the final answer"
    assert by_id[thread]["agent_slug"] == "builder"
    h = by_id[head]
    assert h["kind"] == "head" and h["job_id"] == "j9" and h["rollup"] == "all done"
    assert h["parent_id"] == temp and h["project"] == "alpha"
    assert h["runs_files"] == [f"runs/j9/{head}-head.md"]
    assert set(h) >= {"title", "started_at", "last_at", "running"}
    # limit applies; an agent with no history is an empty list, not a 404
    assert len((await client.get("/api/agents/builder/outputs?limit=1"))
               .json()["outputs"]) == 1
    assert (await client.get("/api/agents/ghost/outputs")).json()["outputs"] == []


async def test_all_outputs_endpoint(client):
    """The all-agents union: every stamped thread plus its unstamped
    descendants, each row once, attributed to the nearest stamped ancestor —
    and reachable at all, despite the `/{slug}` catch-all declared later."""
    from backend.db import open_conversation
    await _make_agent(client)
    a = await _open_chat(agent="builder")
    b = await _open_chat(agent="critic")                  # a deleted agent's past
    plain = await _open_chat()                             # central Jarvis
    db = await get_db()
    try:
        head = await open_conversation(db, project=None, title="[head] y",
                                       kind="head", parent=a, job_id="j1")
        # a child that runs as ANOTHER agent is that agent's output, not a's
        spawned = await open_conversation(db, project=None, title="[critic] z",
                                          kind="agent", parent=a, agent="critic")
        leaf = await open_conversation(db, project=None, title="[sub] w",
                                       kind="subagent", parent=spawned, job_id="j2")
    finally:
        await db.close()

    r = await client.get("/api/agents/outputs")
    assert r.status_code == 200
    rows = r.json()["outputs"]
    owner = {o["id"]: o["owner"] for o in rows}
    assert len(rows) == len(owner)                         # no duplicates
    assert owner == {a: "builder", head: "builder", b: "critic",
                     spawned: "critic", leaf: "critic"}
    assert plain not in owner
    assert all({"snippet", "running", "runs_files", "kind"} <= set(o) for o in rows)
    assert len((await client.get("/api/agents/outputs?limit=2")).json()["outputs"]) == 2


async def test_own_memory_uses_a_private_notes_dir(client, monkeypatch, tmp_env):
    """own_memory was stored and checkboxed and read by nothing. Now a turn
    running as an own_memory agent carries memory_slug on its (host-side)
    envelope, the broker restores it, and memory_write lands in
    agents/<slug>/memory/ — visible via GET /api/agents/<slug>/memory."""
    from backend import chat as chat_mod
    from backend.vm import broker
    await _make_agent(client, own_memory=True)
    seen: list[dict] = []
    monkeypatch.setattr(chat_mod, "guest_turn", _capturing_turn(seen))
    await client.post("/api/chat", json={"message": "hi", "confirm_peak": True,
                                         "agent": "builder"})
    await _settle()
    env = seen[0]["envelope"]
    assert env.memory_slug == "builder"

    broker.register_turn(env)
    try:
        out = await broker.broker_dispatch(env.op_id, "memory_write",
                                           {"name": "Lesson", "content": "private"})
    finally:
        broker.release_turn(env.op_id)
    assert "written" in out["result"]
    assert (tmp_env / "agents" / "builder" / "memory" / "lesson.md").is_file()
    assert not (tmp_env / "memory" / "notes" / "lesson.md").exists()
    notes = (await client.get("/api/agents/builder/memory")).json()["notes"]
    assert [n["name"] for n in notes] == ["lesson"] and "private" in notes[0]["body"]

    # a plain chat keeps the shared notes
    seen.clear()
    await client.post("/api/chat", json={"message": "hi", "confirm_peak": True})
    await _settle()
    assert seen[0]["envelope"].memory_slug is None


async def test_ephemeral_chat_is_not_offered_send_message(client, monkeypatch):
    """send_message has no `requires_project`, so it survived the ephemeral tool
    filter and was offered to an incognito turn that can only ever be refused
    when it calls it. It is dropped from the tool set now (chat.py), so the model
    is not invited to promise a message it cannot send. Asserts ABSENCE, not
    present-but-erroring."""
    from backend import chat as chat_mod
    seen: list[dict] = []
    monkeypatch.setattr(chat_mod, "guest_turn", _capturing_turn(seen))

    r = await client.post("/api/chat", json={"message": "hi", "ephemeral": True})
    assert r.status_code == 200
    await _settle()
    assert len(seen) == 1
    names = {t["function"]["name"] for t in seen[0]["tool_specs"]}
    assert "send_message" not in names, (
        "an incognito turn was handed send_message, which its own handler "
        "refuses — a tool that can only error should not be offered")
    # a persistent chat still gets it, so the filter is scoped to incognito
    seen.clear()
    r = await client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 200
    await _settle()
    names = {t["function"]["name"] for t in seen[0]["tool_specs"]}
    assert "send_message" in names
