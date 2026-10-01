"""Security queue step 2 (SB2, 2026-10-01): every event is linked to its run.

An event may name the conversation it happened in, the top of that
conversation's tree (the run root: one card per run in the Queue), the model's
call id and, for a process event, the box and its boot. raise_event stamps them
from the turn's runtime context; host-side raisers pass them. ATTRIBUTION ONLY:
the actor decision never reads them.
"""
import json
from types import SimpleNamespace

import pytest

from backend import db as db_mod
from backend import egress, runtime, security, writes
from backend.agent import loop as loop_mod
from backend.agent.tools import registry
from backend.config import settings


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    egress._cut.clear()
    egress._stack.clear()
    security._pings.clear()
    (settings.projects_dir / "proj").mkdir(parents=True)
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


async def _conv(db, cid, parent=None, kind="chat", job=None, summary=None):
    await db.execute("INSERT INTO conversations(id, parent_conversation_id, kind, job_id, "
                     "summary) VALUES (?,?,?,?,?)", (cid, parent, kind, job, summary))
    await db.commit()


async def _row(db, eid):
    async with db.execute("SELECT * FROM security_events WHERE id = ?", (eid,)) as cur:
        return dict(await cur.fetchone())


async def test_the_migration_is_idempotent_and_adds_the_columns(tmp_env):
    await db_mod.init_db()
    await db_mod.init_db()
    conn = await db_mod.get_db()
    try:
        async with conn.execute("PRAGMA table_info(security_events)") as cur:
            have = {r["name"] for r in await cur.fetchall()}
        async with conn.execute("PRAGMA table_info(tool_calls)") as cur:
            tc = {r["name"] for r in await cur.fetchall()}
    finally:
        await conn.close()
    assert {"conversation_id", "run_root", "call_id", "box_id", "boot_id"} <= have
    assert "call_id" in tc


async def test_the_run_root_is_the_top_of_the_tree(db):
    await _conv(db, 10)
    await _conv(db, 11, parent=10, kind="head", job="j1")
    await _conv(db, 12, parent=11, kind="agent", job="j1")
    assert await security.run_root_of(db, 12) == 10
    assert await security.run_root_of(db, 10) == 10
    assert await security.run_root_of(db, 999) is None
    assert await security.run_root_of(db, "x") is None


async def test_a_parent_cycle_ends(db):
    await _conv(db, 20)
    await _conv(db, 21, parent=20)
    await db.execute("UPDATE conversations SET parent_conversation_id = 21 WHERE id = 20")
    await db.commit()
    assert await security.run_root_of(db, 20) in (20, 21)


async def test_raise_event_stamps_the_turns_conversation_and_call(db):
    await _conv(db, 30)
    await _conv(db, 31, parent=30, kind="agent", job="j")
    t1 = runtime.conversation_id.set(31)
    t2 = runtime.tool_call_id.set("call_abc")
    try:
        eid = await security.raise_event(db, kind="write_flag", summary="x", project="proj",
                                         detail={"path": "a.py"})
    finally:
        runtime.tool_call_id.reset(t2)
        runtime.conversation_id.reset(t1)
    r = await _row(db, eid)
    assert (r["conversation_id"], r["run_root"], r["call_id"]) == (31, 30, "call_abc")


async def test_the_registrys_call_id_is_read_too(db):
    await _conv(db, 32)
    t1 = runtime.conversation_id.set(32)
    t2 = registry.call_id.set("call_host")
    try:
        eid = await security.raise_event(db, kind="write_flag", summary="y", project="proj")
    finally:
        registry.call_id.reset(t2)
        runtime.conversation_id.reset(t1)
    assert (await _row(db, eid))["call_id"] == "call_host"


async def test_an_explicit_conversation_beats_the_context(db):
    await _conv(db, 40)
    await _conv(db, 41)
    t1 = runtime.conversation_id.set(40)
    try:
        eid = await security.raise_event(db, kind="write_flag", summary="z", project="proj",
                                         conversation_id=41)
    finally:
        runtime.conversation_id.reset(t1)
    r = await _row(db, eid)
    assert (r["conversation_id"], r["run_root"], r["call_id"]) == (41, 41, None)


async def test_no_turn_means_no_attribution(db):
    eid = await security.raise_event(db, kind="login_failed", summary="w")
    r = await _row(db, eid)
    assert r["conversation_id"] is None and r["run_root"] is None


async def test_attribution_never_changes_the_actor_or_the_ping(db):
    """A turn inherits the operator's request context: stamping the run must not
    make an agent's event the operator's, nor an operator's click the agent's."""
    await _conv(db, 50)
    t1 = runtime.conversation_id.set(50)
    try:
        agent = await security.raise_event(db, kind="write_flag", summary="agent did", project="proj")
        mine = await security.raise_event(db, kind="persist_approved", summary="you did",
                                          actor=security.OPERATOR)
    finally:
        runtime.conversation_id.reset(t1)
    a, m = await _row(db, agent), await _row(db, mine)
    assert a["conversation_id"] == 50 and a["actor"] is None
    assert a["acknowledged"] == 0 and a["quiet"] is None
    assert m["actor"] == security.OPERATOR and m["quiet"] == "operator"
    # the same event outside any turn is decided the same way
    outside = await security.raise_event(db, kind="write_flag", summary="agent did 2",
                                         project="proj")
    o = await _row(db, outside)
    assert (o["actor"], o["acknowledged"], o["quiet"]) == (a["actor"], a["acknowledged"],
                                                           a["quiet"])


async def test_the_box_and_boot_come_from_the_detail(db):
    eid = await security.raise_event(db, kind="unexpected_process", summary="p",
                                     detail={"box_id": "p-proj", "boot_id": "b00t"})
    r = await _row(db, eid)
    assert (r["box_id"], r["boot_id"]) == ("p-proj", "b00t")


async def test_a_repeat_from_another_run_is_its_own_row(db):
    await _conv(db, 60)
    await _conv(db, 61)
    first = await security.raise_event(db, kind="write_flag", summary="same", project="proj",
                                       conversation_id=60)
    again = await security.raise_event(db, kind="write_flag", summary="same", project="proj",
                                       conversation_id=60)
    other = await security.raise_event(db, kind="write_flag", summary="same", project="proj",
                                       conversation_id=61)
    assert again == first and other != first
    assert (await _row(db, first))["count"] == 2


async def test_a_repeated_write_flag_keeps_the_latest_fingerprint(db):
    first = await security.raise_event(db, kind="write_flag", summary="s", project="proj",
                                       detail={"path": "a", "sha": "aaa", "bytes": 3})
    await security.raise_event(db, kind="write_flag", summary="s", project="proj",
                               detail={"path": "a", "sha": "bbb", "bytes": 4})
    d = json.loads((await _row(db, first))["detail"])
    assert (d["sha"], d["bytes"]) == ("bbb", 4)


async def test_harness_fault_names_its_conversation(db):
    await _conv(db, 70)
    await security.record_harness_fault(db, tried="t", went_wrong="w", conversation_id=70,
                                        project="proj")
    async with db.execute("SELECT conversation_id, run_root FROM security_events "
                          "WHERE kind = 'harness_fault'") as cur:
        r = await cur.fetchone()
    assert (r["conversation_id"], r["run_root"]) == (70, 70)


async def test_the_tool_sink_stores_the_models_call_id(db):
    await _conv(db, 80)
    sink = loop_mod.db_tool_sink(db, 80)
    await sink("run_code", {"code": "1"}, "ok", "call_one")          # the guest path passes it
    tok = registry.call_id.set("call_two")
    try:
        await sink("run_code", {"code": "2"}, "ok")                    # the host loop sets it
    finally:
        registry.call_id.reset(tok)
    await sink("run_code", {"code": "3"}, "ok")
    async with db.execute("SELECT call_id FROM tool_calls WHERE conversation_id = 80 "
                          "ORDER BY id") as cur:
        assert [r["call_id"] for r in await cur.fetchall()] == ["call_one", "call_two", None]


async def test_a_write_flag_carries_the_run_the_step_and_the_files_fingerprint(db):
    await _conv(db, 90)
    t1 = runtime.conversation_id.set(90)
    t2 = runtime.tool_call_id.set("call_w")
    try:
        await writes.apply_write("proj", "tests/world.test.mjs",
                                 b"test('a', () => {\n  expect(1)\n})\n")
        await writes.apply_write("proj", "tests/world.test.mjs", b"test('a', () => {\n})\n")
    finally:
        runtime.tool_call_id.reset(t2)
        runtime.conversation_id.reset(t1)
    async with db.execute("SELECT * FROM security_events WHERE kind = 'write_flag' "
                          "AND summary LIKE '%assertion_removed%'") as cur:
        r = dict(await cur.fetchone())
    d = json.loads(r["detail"])
    assert (r["conversation_id"], r["run_root"], r["call_id"]) == (90, 90, "call_w")
    import hashlib
    assert d["sha"] == hashlib.sha256(b"test('a', () => {\n})\n").hexdigest()


async def test_procview_names_the_turn_bound_to_a_project_box(db):
    from backend.vm import boxes, procview
    box = SimpleNamespace(id="p-proj", kind="project", joined=False, project="proj",
                          is_shared=False)
    assert procview._bound_conversation(box) is None             # no live turn
    egress.set_context("proj", "op-1", 77)
    boxes.registry._op_box["op-1"] = "p-proj"
    try:
        assert procview._bound_conversation(box) == 77
    finally:
        boxes.registry._op_box.pop("op-1", None)
        egress.clear_context("op-1")


async def test_procview_leaves_a_shared_box_with_two_projects_ambiguous(db):
    from backend.vm import boxes, procview
    box = SimpleNamespace(id=boxes.SHARED_ID, kind="shared", joined=False, project=None,
                          is_shared=True)
    egress.set_context("a", "op-a", 1)
    egress.set_context("b", "op-b", 2)
    try:
        assert procview._bound_conversation(box) is None
    finally:
        egress.clear_context("op-a")
        egress.clear_context("op-b")


async def test_the_proxy_names_the_run_on_a_cut(db, monkeypatch):
    from backend.vm import egress_proxy
    await _conv(db, 95)

    async def drop(host):
        return ["203.0.113.9"]
    monkeypatch.setattr(egress_proxy, "_nft_drop", drop)
    att = {"project": "proj", "kind": "project", "box_id": "p-proj", "op_id": "op-9",
           "conversation_id": 95, "peer_ip": "10.0.0.2", "peer_port": 40000}
    await egress_proxy._record("a8f3k2q9zp1w7v4m.net", "CONNECT", None, 0, 0, "allow",
                               "allow-by-default", att)
    async with db.execute("SELECT * FROM security_events WHERE kind = 'egress_anomaly'") as cur:
        r = dict(await cur.fetchone())
    assert (r["conversation_id"], r["run_root"], r["box_id"]) == (95, 95, "p-proj")
    assert json.loads(r["detail"])["dropped_ips"] == ["203.0.113.9"]
    async with db.execute("SELECT conversation_id FROM egress_events WHERE verdict = 'cut'") as cur:
        assert (await cur.fetchone())["conversation_id"] == 95
