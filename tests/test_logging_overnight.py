"""Raw-context capture is on by default and stored lean: compressed, delta-coded
against the conversation's previous call, aged out a chain at a time; and a
storage watch tells the operator once a day when capture, the database or the
disk gets too big."""
import asyncio
import json
import time

import httpx
import pytest

from backend import bus, ctxstore, storage_watch
from backend.agent.model import record_model_call
from backend.agents_run import NOTICE_CHAN
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, get_state, init_db, set_state
from backend.main import app
from backend.memory import ensure_memory_seeds

USAGE = {"prompt_tokens": 1000, "completion_tokens": 200,
         "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100}


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


def _convo(rounds: int) -> list[list[dict]]:
    """The arrays a ReAct loop sends: the same prefix, grown by a tool round
    (assistant tool call + a chunky tool result) each call."""
    arr = [{"role": "system", "content": "You are Jarvis. " * 200},
           {"role": "user", "content": "read the logs and tell me what broke"}]
    out = []
    for i in range(rounds):
        out.append(list(arr))
        arr += [{"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"c{i}", "type": "function",
                     "function": {"name": "read_file", "arguments": '{"path": "a%d"}' % i}}]},
                {"role": "tool", "tool_call_id": f"c{i}",
                 "content": f"line {i}: " + "something happened here " * 120}]
    return out


async def _all_rows():
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM model_calls ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _load(call_id):
    db = await get_db()
    try:
        return await ctxstore.load(db, call_id)
    finally:
        await db.close()


async def _sql(sql, args=()):
    db = await get_db()
    try:
        await db.execute(sql, args)
        await db.commit()
    finally:
        await db.close()


# --- capture defaults -------------------------------------------------------

async def test_capture_is_on_with_no_state_and_off_only_when_switched_off(tmp_env):
    await init_db()
    db = await get_db()
    try:
        assert await get_state(db, "capture_context") is None
        assert await ctxstore.capture_enabled(db) is True       # never touched: on
        await set_state(db, "capture_context", "0")
        assert await ctxstore.capture_enabled(db) is False      # explicit off is kept
        await set_state(db, "capture_context", "1")
        assert await ctxstore.capture_enabled(db) is True
    finally:
        await db.close()


async def test_init_db_migration_is_idempotent_and_adds_the_chain_column(tmp_env):
    await init_db()
    await init_db()
    db = await get_db()
    try:
        async with db.execute("PRAGMA table_info(model_calls)") as cur:
            assert "ctx_key" in {r["name"] for r in await cur.fetchall()}
        # no migration marker or forced flip: an explicit "0" survives a restart
        await set_state(db, "capture_context", "0")
    finally:
        await db.close()
    await init_db()
    db = await get_db()
    try:
        assert await get_state(db, "capture_context") == "0"
    finally:
        await db.close()


# --- storage form -----------------------------------------------------------

async def test_new_blobs_are_compressed_frames_and_round_trip(tmp_env):
    await init_db()
    msgs = _convo(1)[0]
    await record_model_call(3, "m", USAGE, msgs, tools=[{}, {}])
    row = (await _all_rows())[0]
    assert isinstance(row["context"], bytes) and row["context"][:4] == ctxstore.MAGIC
    raw = len(json.dumps({"messages": msgs, "n_tools": 2}))
    assert len(row["context"]) < raw / 4                # the repeated prompt compresses
    assert await _load(row["id"]) == {"messages": msgs, "n_tools": 2}


async def test_legacy_json_text_rows_still_read(tmp_env):
    await init_db()
    await _sql("INSERT INTO model_calls (conversation_id, context) VALUES (1, ?)",
               (json.dumps({"messages": [{"role": "user", "content": "old"}],
                            "n_tools": 4}),))
    row = (await _all_rows())[0]
    assert isinstance(row["context"], str)
    assert await _load(row["id"]) == {
        "messages": [{"role": "user", "content": "old"}], "n_tools": 4}


async def test_growing_conversation_is_delta_coded_and_every_call_reads_back(tmp_env):
    await init_db()
    arrays = _convo(60)
    for arr in arrays:
        await record_model_call(9, "m", USAGE, arr, tools=[{}] * 3)
    rows = await _all_rows()
    assert len(rows) == 60
    for row, arr in zip(rows, arrays):
        assert await _load(row["id"]) == {"messages": arr, "n_tools": 3}
    stored = sum(len(r["context"]) for r in rows)
    raw = sum(len(json.dumps({"messages": a, "n_tools": 3})) for a in arrays)
    assert stored < raw / 25, (stored, raw)
    # a chain restarts with a full frame at the configured length
    full = [r for r in rows if r["ctx_key"] is None]
    assert len(full) >= 60 // settings.context_delta_chain_max
    assert all(r["ctx_key"] in {f["id"] for f in full} for r in rows if r["ctx_key"])
    chain = {}
    for r in rows:
        chain.setdefault(r["ctx_key"] or r["id"], []).append(r)
    assert max(map(len, chain.values())) <= settings.context_delta_chain_max + 1


async def test_a_rewritten_prefix_starts_a_full_frame(tmp_env):
    """Compaction replaces the front of the context: nothing shared, so no delta."""
    await init_db()
    a = _convo(3)
    await record_model_call(4, "m", USAGE, a[2], tools=None)
    rewritten = [{"role": "system", "content": "different " * 500}] + a[2][1:]
    await record_model_call(4, "m", USAGE, rewritten, tools=None)
    rows = await _all_rows()
    assert rows[1]["ctx_key"] is None
    assert (await _load(rows[1]["id"]))["messages"] == rewritten


async def test_delta_off_stores_full_compressed_frames(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "context_capture_delta", False)
    await init_db()
    for arr in _convo(4):
        await record_model_call(2, "m", USAGE, arr, tools=None)
    rows = await _all_rows()
    assert all(r["ctx_key"] is None for r in rows)
    assert (await _load(rows[3]["id"]))["messages"] == _convo(4)[3]


async def test_after_a_restart_the_first_call_is_a_full_frame(tmp_env):
    await init_db()
    arrays = _convo(3)
    await record_model_call(6, "m", USAGE, arrays[0], tools=None)
    await record_model_call(6, "m", USAGE, arrays[1], tools=None)
    ctxstore.forget_heads()                      # process restarted
    await record_model_call(6, "m", USAGE, arrays[2], tools=None)
    rows = await _all_rows()
    assert rows[1]["ctx_key"] == rows[0]["id"] and rows[2]["ctx_key"] is None
    assert (await _load(rows[2]["id"]))["messages"] == arrays[2]


async def test_two_calls_racing_in_one_conversation_both_read_back(tmp_env):
    await init_db()
    arrays = _convo(3)
    await record_model_call(8, "m", USAGE, arrays[0], tools=None)
    await asyncio.gather(
        record_model_call(8, "m", USAGE, arrays[1], tools=None),
        record_model_call(8, "m", USAGE, arrays[2], tools=None))
    rows = await _all_rows()
    got = {json.dumps((await _load(r["id"]))["messages"]) for r in rows}
    assert got == {json.dumps(a) for a in arrays}


async def test_images_are_redacted_before_storage(tmp_env):
    await init_db()
    big = "data:image/png;base64," + "A" * 5000
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": big}}]}]
    await record_model_call(1, "m", USAGE, msgs, tools=None)
    row = (await _all_rows())[0]
    ctx = await _load(row["id"])
    assert "AAAA" not in json.dumps(ctx)
    assert "image redacted" in json.dumps(ctx)


async def test_incognito_stores_nothing_and_a_serialisation_failure_keeps_the_usage_row(tmp_env):
    await init_db()
    from backend import runtime
    tok = runtime.ephemeral.set(True)
    try:
        await record_model_call(1, "m", USAGE, _convo(1)[0], tools=None)
    finally:
        runtime.ephemeral.reset(tok)
    # a message json.dumps cannot handle even with default=str
    bad = {"role": "user", "content": {(1, 2): "tuple key"}}
    await record_model_call(1, "m", USAGE, [bad], tools=None)
    rows = await _all_rows()
    assert [r["context"] for r in rows] == [None, None]
    assert all(r["input_tokens"] == 1000 for r in rows)


# --- retention --------------------------------------------------------------

async def test_a_chain_ages_out_as_a_unit_and_never_half(tmp_env):
    await init_db()
    arrays = _convo(6)
    for arr in arrays:
        await record_model_call(5, "m", USAGE, arr, tools=None)
    ids = [r["id"] for r in await _all_rows()]
    # the first four are 30 days old, the last two are fresh — one chain
    await _sql("UPDATE model_calls SET created_at = datetime('now', '-30 days') "
               "WHERE id IN (%s)" % ",".join(map(str, ids[:4])))
    db = await get_db()
    try:
        assert await ctxstore.prune(db, 7) == 0             # newest row still live
        await db.commit()
    finally:
        await db.close()
    for i, arr in zip(ids, arrays):
        assert (await _load(i))["messages"] == arr          # nothing lost
    await _sql("UPDATE model_calls SET created_at = datetime('now', '-30 days')")
    db = await get_db()
    try:
        assert await ctxstore.prune(db, 7) == 6             # whole chain now old
        await db.commit()
    finally:
        await db.close()
    assert all(r["context"] is None for r in await _all_rows())
    assert await _load(ids[3]) is None
    assert all(r["input_tokens"] == 1000 for r in await _all_rows())   # usage kept


async def test_a_chain_with_a_missing_frame_is_reported_gone_not_garbage(client):
    arrays = _convo(4)
    for arr in arrays:
        await record_model_call(5, "m", USAGE, arr, tools=None)
    rows = await _all_rows()
    await _sql("UPDATE model_calls SET context = NULL WHERE id = ?", (rows[1]["id"],))
    db = await get_db()
    try:
        with pytest.raises(ctxstore.ContextGone):
            await ctxstore.load(db, rows[3]["id"])
    finally:
        await db.close()
    r = await client.get(f"/api/logs/calls/{rows[3]['id']}/context")
    assert r.status_code == 404 and "gone" in r.json()["detail"]
    ok = await client.get(f"/api/logs/calls/{rows[0]['id']}/context")
    assert ok.status_code == 200 and ok.json()["messages"] == arrays[0]


async def test_the_ledger_insert_applies_the_chosen_retention_days(tmp_env):
    await init_db()
    await _sql("INSERT INTO model_calls (conversation_id, context, created_at) "
               "VALUES (1, '{\"messages\": []}', datetime('now', '-3 days'))")
    await record_model_call(1, "m", USAGE, _convo(1)[0], tools=None)
    assert (await _all_rows())[0]["context"] is not None    # 3 days < default 7
    ctxstore._last_prune.clear()
    await _sql("INSERT INTO session_state (key, value) VALUES ('context_keep_days', '1')")
    await record_model_call(1, "m", USAGE, _convo(1)[0], tools=None)
    assert (await _all_rows())[0]["context"] is None        # 3 days > chosen 1


# --- endpoints --------------------------------------------------------------

async def test_retention_and_delete_endpoints(client):
    for arr in _convo(3):
        await record_model_call(5, "m", USAGE, arr, tools=None)
    await _sql("UPDATE model_calls SET created_at = datetime('now', '-10 days')")

    bad = await client.post("/api/logs/capture-retention", json={"days": 5})
    assert bad.status_code == 400
    st = (await client.get("/api/logs/storage")).json()
    assert st["capture_on"] is True and st["keep_days"] == 7
    assert st["captured_calls"] == 3 and st["captured_bytes"] > 0

    # a 14 day choice keeps the 10 day old context; it is remembered
    r = (await client.post("/api/logs/capture-retention", json={"days": 14})).json()
    assert r["deleted"] == 0
    assert (await client.get("/api/logs/storage")).json()["keep_days"] == 14
    # ...and a shorter one deletes it at once
    r = (await client.post("/api/logs/capture-retention", json={"days": 7})).json()
    assert r["deleted"] == 3 and r["freed_bytes"] > 0
    st = (await client.get("/api/logs/storage")).json()
    assert st["captured_calls"] == 0 and st["captured_bytes"] == 0

    for arr in _convo(2):
        await record_model_call(5, "m", USAGE, arr, tools=None)
    await _sql("UPDATE model_calls SET created_at = datetime('now', '-3 days') "
               "WHERE id > 3")
    bad = await client.post("/api/logs/prune-context", json={"older_than_days": 0})
    assert bad.status_code == 400
    done = (await client.post("/api/logs/prune-context",
                              json={"older_than_days": 1})).json()
    assert done["deleted"] == 2 and done["captured_bytes"] == 0
    # usage rows survive both
    costs = (await client.get("/api/logs/costs")).json()
    assert costs["windows"]["all"]["calls"] == 5


async def test_every_call_of_a_conversation_is_viewable_with_nothing_switched_on(client):
    arrays = _convo(5)
    for arr in arrays:
        await record_model_call(11, "m", USAGE, arr, tools=[{}])
    calls = (await client.get("/api/logs/conversations/11/calls")).json()["calls"]
    assert [c["has_context"] for c in calls] == [True] * 5
    for c, arr in zip(calls, arrays):
        got = (await client.get(f"/api/logs/calls/{c['id']}/context")).json()
        assert got["messages"] == arr and got["n_tools"] == 1


# --- storage watch ----------------------------------------------------------

def _st(**kw):
    base = dict(captured_bytes=0, captured_calls=0, captured_oldest=None,
                db_file_bytes=0, db_reusable_bytes=0, db_used_bytes=0,
                disk_total_bytes=100 * 2**30, disk_free_bytes=50 * 2**30,
                disk_free_pct=50.0)
    base.update(kw)
    return base


def test_over_names_each_limit_that_is_exceeded():
    assert storage_watch.over(_st()) == []
    got = storage_watch.over(_st(
        captured_bytes=2 * 2**30, captured_calls=41200,
        db_used_bytes=4 * 2**30, db_reusable_bytes=2**30,
        disk_free_pct=8.0, disk_free_bytes=8 * 2**30))
    assert [r["kind"] for r in got] == ["captured", "db", "disk"]
    text = storage_watch.message(got)
    assert "2.0 GB across 41,200 calls" in text and "8% free" in text
    assert text.endswith("Lower retention or delete older captured context "
                         "on Security > Logs > Cost.")
    # the numbers are the settings, not constants
    assert storage_watch.over(_st(captured_bytes=2**30 + 1)) != []
    assert storage_watch.over(_st(captured_bytes=2**30)) == []


def test_fmt_bytes():
    f = storage_watch.fmt_bytes
    assert f(512) == "512 B" and f(45_600) == "45 KB"
    assert f(412 * 2**20) == "412 MB" and f(1.3 * 2**30) == "1.3 GB"


async def test_check_notifies_once_a_day_while_over_and_is_not_a_security_event(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "storage_captured_warn_mb", 0)   # anything is over
    q = bus.subscribe(NOTICE_CHAN)
    try:
        await record_model_call(1, "m", USAGE, _convo(1)[0], tools=None)
        t0 = 1_000_000.0
        first = await storage_watch.check(now=t0)
        assert first["notified"] is True
        assert [r["kind"] for r in first["over"]] == ["captured"]
        ev = q.get_nowait()
        assert ev["type"] == "storage_warning" and ev["to"] == "/security/logs"
        assert "Lower retention or delete older captured context" in ev["summary"]
        assert q.empty()

        again = await storage_watch.check(now=t0 + 3600)           # an hour later
        assert again["notified"] is False and q.empty()
        day = await storage_watch.check(now=t0 + 86400 + 1)         # a day later, still over
        assert day["notified"] is True and q.get_nowait()["type"] == "storage_warning"

        db = await get_db()
        try:
            async with db.execute("SELECT COUNT(*) AS n FROM security_events") as cur:
                assert (await cur.fetchone())["n"] == 0
        finally:
            await db.close()
    finally:
        bus.unsubscribe(NOTICE_CHAN, q)


async def test_check_is_quiet_under_the_limits_and_status_reports_the_picture(tmp_env):
    await init_db()
    q = bus.subscribe(NOTICE_CHAN)
    try:
        await record_model_call(1, "m", USAGE, _convo(1)[0], tools=None)
        res = await storage_watch.check()
        assert res["notified"] is False and res["over"] == [] and q.empty()
        st = await storage_watch.status()
        assert st["capture_on"] is True and st["keep_days"] == 7
        assert st["captured_calls"] == 1 and st["over"] == [] and st["message"] is None
        assert st["disk_total_bytes"] > 0 and st["db_file_bytes"] > 0
        assert st["limits"]["free_pct"] == settings.storage_free_warn_pct
        assert st["last_checked"] is not None and st["last_notified"] is None
    finally:
        bus.unsubscribe(NOTICE_CHAN, q)


async def test_check_applies_retention(tmp_env):
    await init_db()
    await _sql("INSERT INTO model_calls (conversation_id, context, created_at) "
               "VALUES (1, '{\"messages\": []}', datetime('now', '-30 days'))")
    res = await storage_watch.check()
    assert res["stats"]["captured_calls"] == 0
    assert (await _all_rows())[0]["context"] is None


async def test_disk_low_alone_notifies(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "storage_free_warn_pct", 101.0)   # always "low"
    q = bus.subscribe(NOTICE_CHAN)
    try:
        res = await storage_watch.check(now=time.time())
        assert [r["kind"] for r in res["over"]] == ["disk"] and res["notified"]
        assert "free" in q.get_nowait()["summary"]
    finally:
        bus.unsubscribe(NOTICE_CHAN, q)


async def test_watch_loop_does_nothing_when_disabled(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "storage_watch_enabled", False)
    await asyncio.wait_for(storage_watch.watch_loop(), timeout=2)   # returns at once
