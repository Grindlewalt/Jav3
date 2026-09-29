"""Security > Calls, host side: the model_calls ledger learns which operation and
which box made each call, the gateway logs what it refuses, and GET
/api/logs/calls serves both as one timeline. Offline: the guest is an AF_UNIX
socketpair and the model transport is scripted."""
import asyncio
import json
import socket

import httpx
import pytest

from backend import runtime
from backend.agent import budget as bmod
from backend.agent.budget import Budget
from backend.agent.model import Model, model, record_model_call
from backend.auth import hash_password
from backend.config import settings
from backend.db import _migrate_calls, get_db, init_db
from backend.main import app
from backend.vm import boxes, broker, gateway_log
from backend.vm.gateway_server import handle_conn

USAGE = {"prompt_tokens": 1000, "completion_tokens": 200,
         "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100}
MSGS = [{"role": "user", "content": "hi"}]


async def _q(sql, args=()):
    db = await get_db()
    try:
        async with db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _x(sql, args=()):
    db = await get_db()
    try:
        await db.execute(sql, args)
        await db.commit()
    finally:
        await db.close()


# --- migration -------------------------------------------------------------

async def test_migrate_calls_is_idempotent_and_keeps_old_rows(tmp_env):
    # a database from before this change: model_calls without the two columns
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = await get_db()
    try:
        await db.execute(
            "CREATE TABLE model_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "conversation_id INTEGER, model TEXT, input_tokens INTEGER NOT NULL "
            "DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, cache_hit INTEGER "
            "NOT NULL DEFAULT 0, cache_miss INTEGER NOT NULL DEFAULT 0, context TEXT, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        await db.execute("INSERT INTO model_calls (conversation_id, model) VALUES (3, 'm')")
        await db.commit()
        await _migrate_calls(db)
        await _migrate_calls(db)          # a second run adds nothing and fails nothing
        await db.commit()
    finally:
        await db.close()
    cols = {r["name"] for r in await _q("PRAGMA table_info(model_calls)")}
    assert {"op_id", "box_id"} <= cols
    row = (await _q("SELECT * FROM model_calls"))[0]
    assert row["conversation_id"] == 3 and row["op_id"] is None and row["box_id"] is None
    rcols = {r["name"] for r in await _q("PRAGMA table_info(gateway_refusals)")}
    assert rcols == {"id", "ts", "op_name", "reason", "box_id", "project_slug"}


async def test_init_db_twice_is_fine(tmp_env):
    await init_db()
    await init_db()
    assert {"op_id", "box_id"} <= {r["name"] for r in await _q("PRAGMA table_info(model_calls)")}


# --- the ledger row --------------------------------------------------------

async def test_record_model_call_carries_op_id_and_box_id(tmp_env):
    await init_db()
    await record_model_call(7, "deepseek/deepseek-flash", USAGE, MSGS, None,
                            op_id="guest:7", box_id="p-homelab")
    await record_model_call(8, "m", USAGE, MSGS, None)          # a host-side call
    a, b = await _q("SELECT * FROM model_calls ORDER BY id")
    assert (a["op_id"], a["box_id"]) == ("guest:7", "p-homelab")
    assert (b["op_id"], b["box_id"]) == (None, None)


async def test_incognito_call_keeps_no_op_id(tmp_env):
    # op ids are chat:<cid> / guest:<cid>: keeping one would name the chat
    await init_db()
    tok = runtime.ephemeral.set(True)
    try:
        await record_model_call(7, "m", USAGE, MSGS, None, op_id="guest:7", box_id="shared")
    finally:
        runtime.ephemeral.reset(tok)
    row = (await _q("SELECT * FROM model_calls"))[0]
    assert row["conversation_id"] is None and row["op_id"] is None
    assert row["box_id"] == "shared"


async def test_odd_ids_are_stored_short(tmp_env):
    await init_db()
    await record_model_call(1, "m", USAGE, MSGS, None, op_id="x" * 500, box_id="y" * 500)
    row = (await _q("SELECT * FROM model_calls"))[0]
    assert len(row["op_id"]) == 80 and len(row["box_id"]) == 80


# --- the gateway -----------------------------------------------------------

def _script(monkeypatch, content="PONG"):
    async def fake_stream_once(self, base, key, payload):
        yield {"type": "raw", "content": content, "tool_calls": [], "usage": USAGE}
    monkeypatch.setattr(Model, "_stream_once", fake_stream_once)
    monkeypatch.setattr(model, "api_key", "sk-secret")
    monkeypatch.setattr(model.transport, "api_key", "sk-secret")


async def _send(req: dict, **kw) -> list[dict]:
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(handle_conn(loop, b, **kw))
    try:
        await loop.sock_sendall(a, (json.dumps(req) + "\n").encode())
        data = b""
        while not any(t in data for t in (b'"message"', b'"error"', b'"pong"')):
            chunk = await asyncio.wait_for(loop.sock_recv(a, 1 << 20), timeout=5)
            if not chunk:
                break
            data += chunk
        return [json.loads(x) for x in data.splitlines() if x.strip()]
    finally:
        a.close()
        await asyncio.wait_for(task, timeout=5)


@pytest.fixture
async def gw(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_max_boxes", 8)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 3)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 10**6)
    boxes.registry.reset()
    yield
    boxes.registry.reset()


def _turn(op_id, box=None, slug="homelab", budget=None):
    bmod.register(op_id, budget or Budget(10**9, 10**9))
    broker.register_token(op_id, "tok")
    broker.register_turn(broker.TurnEnvelope(op_id=op_id, active_project=slug))
    if box is not None:
        boxes.bind_op(op_id, box, slug)


def _done(op_id):
    bmod.release(op_id)
    broker.release_token(op_id)


async def test_a_served_call_names_its_op_and_box(gw, monkeypatch):
    _script(monkeypatch)
    box = boxes.allocate("project", project="homelab")
    _turn("guest:42", box)
    try:
        ev = await _send({"op": "model_call", "op_id": "guest:42", "op_token": "tok",
                          "conversation_id": 42, "messages": MSGS}, box=box)
    finally:
        _done("guest:42")
    assert [e for e in ev if e["type"] == "message"]
    row = (await _q("SELECT * FROM model_calls"))[0]
    assert row["op_id"] == "guest:42" and row["box_id"] == box.id
    assert row["conversation_id"] == 42
    assert not await _q("SELECT * FROM gateway_refusals")     # nothing was refused


async def test_box_id_does_not_leak_into_the_next_host_call(gw, monkeypatch):
    from backend.agent.model import call_box_id
    _script(monkeypatch)
    box = boxes.allocate("project", project="homelab")
    _turn("guest:42", box)
    try:
        await _send({"op": "model_call", "op_id": "guest:42", "op_token": "tok",
                     "messages": MSGS}, box=box)
    finally:
        _done("guest:42")
    assert call_box_id.get() is None          # reset after the served call


async def test_refusals_are_logged(gw, monkeypatch):
    _script(monkeypatch)
    box = boxes.allocate("project", project="homelab")
    other = boxes.allocate("project", project="other")
    svc = boxes.allocate("service", project="homelab")
    _turn("guest:1", box)
    _turn("guest:2", box, budget=Budget(1, 1))
    bmod.get("guest:2").add({"prompt_tokens": 5, "completion_tokens": 5})   # spent
    try:
        # an op_id the host never registered
        r = await _send({"op": "model_call", "op_id": "nope", "op_token": "x",
                         "messages": MSGS}, box=box)
        assert r[0]["error"] == "unknown_op_id"
        # a live turn asked for from the wrong box
        r = await _send({"op": "model_call", "op_id": "guest:1", "op_token": "tok",
                         "messages": MSGS}, box=other)
        assert r[0]["error"] == "unknown_op_id"
        # a spent budget
        r = await _send({"op": "model_call", "op_id": "guest:2", "op_token": "tok",
                         "messages": MSGS}, box=box)
        assert r[0]["error"] == "BudgetExceeded"
        # a kind that may not call the model
        r = await _send({"op": "model_call", "op_id": "guest:1", "op_token": "tok"}, box=svc)
        assert r[0]["error"] == "op_not_allowed"
        # an op no kind may use, and a broker call with a bad token
        assert (await _send({"op": "frobnicate"}, box=box))[0]["error"] == "op_not_allowed"
        r = await _send({"op": "tool_broker_call", "op_id": "guest:1", "op_token": "bad",
                         "name": "x", "args": {}}, box=box)
        assert r[0]["error"] == "unknown_op_id"
    finally:
        _done("guest:1")
        _done("guest:2")
    rows = await _q("SELECT * FROM gateway_refusals ORDER BY id")
    got = [(r["op_name"], r["reason"], r["box_id"], r["project_slug"]) for r in rows]
    assert got == [
        ("model_call", "unknown_op_id", box.id, "homelab"),
        ("model_call", "wrong_box", other.id, "homelab"),
        ("model_call", "budget_exceeded", box.id, "homelab"),
        ("model_call", "op_not_allowed", svc.id, "homelab"),
        ("frobnicate", "op_not_allowed", box.id, "homelab"),
        ("tool_broker_call", "unknown_op_id", box.id, "homelab"),
    ]
    assert not await _q("SELECT * FROM model_calls")           # nothing reached the model
    assert "tok" not in json.dumps(rows) and "sk-secret" not in json.dumps(rows)


async def test_unknown_op_is_logged_when_boxes_are_off(tmp_env):
    await init_db()
    assert settings.vm_boxes_enabled is False
    r = await _send({"op": "frobnicate"})
    assert r[0]["error"] == "unknown_op"
    row = (await _q("SELECT * FROM gateway_refusals"))[0]
    assert (row["op_name"], row["reason"], row["box_id"]) == ("frobnicate", "unknown_op", None)


async def test_refusal_log_is_capped_and_printable(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(gateway_log, "KEEP", 3)
    for i in range(6):
        await gateway_log.record_refusal(f"op{i}\x1b[31m\n" + "z" * 300, "unknown_op")
    rows = await _q("SELECT * FROM gateway_refusals ORDER BY id")
    assert len(rows) == 3
    assert all(len(r["op_name"]) <= 80 and "\x1b" not in r["op_name"]
               and "\n" not in r["op_name"] for r in rows)
    assert rows[-1]["op_name"].startswith("op5")


async def test_a_failing_log_never_breaks_the_gateway(tmp_env):
    # no init_db: the table does not exist; record_refusal swallows it
    await gateway_log.record_refusal("model_call", "unknown_op_id")


# --- GET /api/logs/calls ---------------------------------------------------

@pytest.fixture
async def client(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-secret")
    await init_db()
    await _x("INSERT INTO users (username, password_hash) VALUES (?, ?)",
             ("operator", hash_password("hunter2")))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        yield c


async def _seed():
    """Times are relative to now: a call an hour ago, one two hours ago, a
    refusal three hours ago, and one of each five days ago (outside 24h)."""
    await _x("INSERT INTO projects (id, slug, name, path) "
             "VALUES (1, 'homelab', 'Homelab', '/tmp/homelab')")
    await _x("INSERT INTO conversations (id, kind, project_id) VALUES (42, 'chat', 1)")
    await _x("INSERT INTO conversations (id, kind) VALUES (43, 'chat')")
    ins = ("INSERT INTO model_calls (conversation_id, model, input_tokens, output_tokens, "
           "cache_hit, cache_miss, context, op_id, box_id, created_at) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?))")
    await _x(ins, (42, "deepseek/deepseek-flash", 12431, 812, 9102, 3329, '{"messages": []}',
                   "guest:42", "p-homelab", "-1 hours"))
    await _x(ins, (43, "deepseek/deepseek-flash", 1000, 100, 0, 1000, None,
                   "chat:43", None, "-2 hours"))
    await _x(ins, (42, "deepseek/deepseek-flash", 500, 50, 0, 500, None,
                   "guest:42", "p-homelab", "-5 days"))
    ref = ("INSERT INTO gateway_refusals (ts, op_name, reason, box_id, project_slug) "
           "VALUES (datetime('now', ?), 'model_call', ?, ?, ?)")
    await _x(ref, ("-3 hours", "unknown_op_id", "p-homelab", "homelab"))
    await _x(ref, ("-5 days", "wrong_box", "shared", None))


async def test_calls_endpoint_shape(client):
    await _seed()
    r = await client.get("/api/logs/calls")
    assert r.status_code == 200
    d = r.json()
    assert d["hours"] == 24 and d["conversation_id"] is None
    assert d["key_hosts"] == ["api.deepseek.com"]
    assert d["totals"]["calls"] == 2 and d["totals"]["refused"] == 1
    assert d["totals"]["cost_usd"] > 0
    assert [x["kind"] for x in d["rows"]] == ["call", "call", "refused"]   # merged, newest first
    assert [x["ts"] for x in d["rows"]] == sorted((x["ts"] for x in d["rows"]), reverse=True)
    call = d["rows"][0]
    assert call["op_id"] == "guest:42" and call["box_id"] == "p-homelab"
    assert call["conversation_id"] == 42 and call["project_slug"] == "homelab"
    assert call["input_tokens"] == 12431 and call["output_tokens"] == 812
    assert call["cache_hit"] == 9102 and call["has_context"] is True
    assert call["cost_usd"] > 0 and d["rows"][1]["has_context"] is False
    assert d["rows"][1]["project_slug"] is None
    ref = d["rows"][2]
    assert (ref["op_name"], ref["reason"], ref["box_id"], ref["project_slug"]) == \
        ("model_call", "unknown_op_id", "p-homelab", "homelab")
    assert d["truncated"] is False
    assert "sk-secret" not in r.text                               # the key never leaves


async def test_calls_endpoint_window_and_conversation_filter(client):
    await _seed()
    wide = (await client.get("/api/logs/calls", params={"hours": 24 * 10})).json()
    assert wide["totals"]["calls"] == 3 and wide["totals"]["refused"] == 2
    d = (await client.get("/api/logs/calls", params={"conversation_id": 42})).json()
    assert [x["conversation_id"] for x in d["rows"]] == [42]
    assert d["totals"]["calls"] == 1 and d["totals"]["refused"] == 0   # refusals name no chat
    assert d["conversation_id"] == 42
    # a window is at least an hour and at most a year, whatever is asked for
    assert (await client.get("/api/logs/calls", params={"hours": 0})).json()["hours"] == 1
    assert (await client.get("/api/logs/calls", params={"hours": 10**9})).json()["hours"] == 8760


async def test_calls_endpoint_caps_rows_and_says_so(client):
    await _seed()
    d = (await client.get("/api/logs/calls", params={"limit": 2})).json()
    assert len(d["rows"]) == 2 and d["truncated"] is True
    assert d["totals"]["calls"] == 2 and d["totals"]["refused"] == 1


async def test_calls_endpoint_empty(client):
    d = (await client.get("/api/logs/calls")).json()
    assert d["rows"] == [] and d["totals"] == {"calls": 0, "cost_usd": 0, "refused": 0}
    assert d["key_hosts"] == ["api.deepseek.com"]


async def test_calls_endpoint_names_no_host_without_a_key(client, monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    assert (await client.get("/api/logs/calls")).json()["key_hosts"] == []


async def test_calls_endpoint_needs_a_login(tmp_env):
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/logs/calls")).status_code == 401
