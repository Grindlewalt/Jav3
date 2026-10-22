"""send_message to several recipients, and to every item of the plan at once.

2026-09-30 benchmark run: the orchestrator sent eight identical "PRE-FLIGHT"
messages, one send_message per plan item, in the same second. `to` now takes a
list, and "items" names every item that is running or has not started. One
message is still stored per recipient, and the old single-address call is
byte-for-byte what it was.
"""
import importlib.util
from pathlib import Path

import httpx
import pytest

from backend import runtime
from backend import plan as plan_mod
from backend.auth import hash_password
from backend.db import get_db, init_db, open_conversation
from backend.main import app
from backend.memory import ensure_memory_seeds
from backend.vm import broker

REPO = Path(__file__).resolve().parents[1]
SLUG = "alpha"


def _handler():
    spec = importlib.util.spec_from_file_location(
        "t_send_message", REPO / "tools" / "send_message" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
async def client(tmp_env):
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
    plan_mod._live_items.clear()
    for cid in list(_live):
        broker.release_turn(_live.pop(cid))


_live: dict = {}     # conversation id -> op id of the turn registered for it


async def _plan(client, statuses: dict) -> dict:
    """A plan with one item per entry of `statuses` ({id: status}), and a live
    conversation for every running one. Returns {item id: conversation id}."""
    items = [{"title": f"item {i}", "brief": "b"} for i in statuses]
    r = await client.put(f"/api/projects/{SLUG}/plan", json={"items": items})
    assert r.status_code == 200, r.text
    cids = {}
    db = await get_db()
    try:
        for iid, st in statuses.items():
            if st == "running":
                cids[iid] = await open_conversation(db, project=SLUG, title=iid, kind="agent")
                plan_mod._live_items[cids[iid]] = {"project": SLUG, "item_id": iid, "title": iid}
                env = broker.TurnEnvelope(op_id=f"t:{cids[iid]}", conversation_id=cids[iid],
                                          active_project=SLUG)
                broker.register_turn(env)           # a turn in flight: a message reaches it now
                _live[cids[iid]] = env.op_id
    finally:
        await db.close()
    async with plan_mod.edit(SLUG) as plan:
        for it in plan["items"]:
            it["status"] = statuses[it["id"]]
            if it["id"] in cids:
                it["conversation_id"] = cids[it["id"]]
    return cids


async def _chat() -> int:
    db = await get_db()
    try:
        return await open_conversation(db, project=SLUG, title="orchestrator", kind="chat")
    finally:
        await db.close()


async def _send(cid: int, to, message="mind the schema"):
    tok = runtime.conversation_id.set(cid)
    try:
        return await _handler().run(to, message)
    finally:
        runtime.conversation_id.reset(tok)


async def _rows():
    db = await get_db()
    try:
        async with db.execute("SELECT to_conversation_id, body, from_label FROM agent_messages "
                              "ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def test_items_reaches_every_running_or_waiting_item_once_each(client):
    cids = await _plan(client, {"i1": "running", "i2": "running", "i3": "todo",
                                "i4": "done", "i5": "failed", "i6": "skipped"})
    orch = await _chat()
    out = await _send(orch, "items", "PRE-FLIGHT: use the shared schema")
    assert out.startswith("Sent to 3 of 3 (one message each):"), out
    assert "item:i1, item:i2" in out and "item:i3" in out
    for settled in ("item:i4", "item:i5", "item:i6"):
        assert settled not in out
    rows = await _rows()
    assert sorted(r["to_conversation_id"] for r in rows if r["to_conversation_id"]) == \
        sorted(cids.values())                         # one stored row per live item
    assert len(rows) == 2                             # the todo item took a note instead
    assert all(r["body"] == "PRE-FLIGHT: use the shared schema" for r in rows)
    assert all(r["from_label"] == "jav3" for r in rows)
    notes = plan_mod.load(SLUG)["items"][2]["notes"]
    assert [n["body"] for n in notes] == ["PRE-FLIGHT: use the shared schema"]


async def test_a_list_mixes_items_and_agents_and_names_who_could_not_be_reached(client, tmp_env):
    cids = await _plan(client, {"i1": "running", "i2": "done"})
    d = tmp_env / "agents" / "builder"
    d.mkdir(parents=True)
    (d / "AGENT.md").write_text("---\nname: builder\ndescription: d\n---\nYou are builder.")
    orch = await _chat()
    out = await _send(orch, ["item:i1", "builder", "item:i2", "nobody-here", "builder"])
    assert out.startswith("Sent to 2 of 4 (one message each):"), out
    assert "item:i1 — running now" in out and "builder — not running" in out
    assert "Not sent:" in out and "item:i2 —" in out and "nobody-here —" in out
    rows = await _rows()
    assert len(rows) == 2                      # "builder" twice is one message
    assert {r["to_conversation_id"] for r in rows} == {cids["i1"], None}


async def test_a_plan_item_broadcasting_leaves_itself_out(client):
    cids = await _plan(client, {"i1": "running", "i2": "running", "i3": "todo"})
    out = await _send(cids["i1"], "items", "i1 here: schema is in db.py")
    assert out.startswith("Sent to 2 of 2"), out
    assert "item:i1" not in out and "item:i2" in out and "item:i3" in out
    assert [r["to_conversation_id"] for r in await _rows()] == [cids["i2"]]


async def test_items_without_a_plan_or_with_nothing_left_says_so(client):
    orch = await _chat()
    out = await _send(orch, "items")
    assert out.startswith("error:") and "has no plan" in out
    await _plan(client, {"i1": "done", "i2": "failed"})
    out = await _send(orch, ["items"])
    assert out.startswith("error:") and "running or waiting to start" in out
    assert await _rows() == []


async def test_the_single_address_call_is_unchanged(client, tmp_env):
    cids = await _plan(client, {"i1": "running"})
    orch = await _chat()
    one = await _send(orch, "item:i1", "hello")
    assert one.startswith("sent to conversation") and "running now" in one
    assert (await _send(orch, [str(cids["i1"])], "again")).startswith("sent to conversation")
    # the lookup keeps the message and lists addresses, in either spelling
    for to in ("?", ["?"], ["item:i1", "?"]):
        out = await _send(orch, to, "keep me")
        assert out.startswith("error: Your message was not sent")
    assert len(await _rows()) == 2
    assert (await _send(orch, "", "x")).startswith("error: Your message was not sent")


async def test_a_list_needs_text_and_stays_within_the_plan_size(client):
    await _plan(client, {"i1": "running"})
    orch = await _chat()
    assert "needs the message text" in await _send(orch, ["item:i1", "builder"], "  ")
    many = [str(900 + n) for n in range(40)]
    assert "over the limit" in await _send(orch, many)
    assert await _rows() == []
    # a list the model sent as JSON text, and comma-separated ids, are read as lists
    out = await _send(orch, '["item:i1"]', "json text")
    assert out.startswith("sent to conversation")
    out = await _send(orch, "item:i1, item:i1", "comma list")
    assert out.startswith("Sent to 1 of 1")                 # twice named, one recipient


async def test_a_refusal_for_everyone_is_one_plain_error(client):
    await _plan(client, {"i1": "running", "i2": "running"})
    out = await _handler().run("items", "no identity")      # no turn: nobody to send as
    assert out.startswith("error:") and "no turn identity" in out
    assert await _rows() == []


def test_the_schema_offers_a_list_and_the_handler_takes_one():
    import yaml
    text = (REPO / "tools" / "send_message" / "TOOL.md").read_text()
    front = yaml.safe_load(text.split("---")[1])
    to = front["parameters"]["properties"]["to"]
    assert to["type"] == "array" and to["items"] == {"type": "string"}
    assert '"items"' in to["description"] and "to=" in text


async def test_a_guest_call_with_a_list_goes_through_the_gateway(client):
    """The guest's send_message is brokered to the host: a list `to` survives
    the argument check, and the sender is still the envelope's."""
    from tests.test_agent_comms import _Envelope, _gateway_call
    cids = await _plan(client, {"i1": "running", "i2": "running"})
    orch = await _chat()
    with _Envelope("op-orch", orch, project=SLUG):
        out = await _gateway_call("op-orch", "send_message",
                                  {"to": ["item:i1", "item:i2"], "message": "both of you"})
        out2 = await _gateway_call("op-orch", "send_message",
                                   {"to": "items", "message": "all of you"})
    assert out["type"] == "broker_result" and out["result"].startswith("Sent to 2 of 2"), out
    assert out2["result"].startswith("Sent to 2 of 2"), out2
    rows = await _rows()
    assert [(r["to_conversation_id"], r["body"]) for r in rows] == [
        (cids["i1"], "both of you"), (cids["i2"], "both of you"),
        (cids["i1"], "all of you"), (cids["i2"], "all of you")]
    assert {r["from_label"] for r in rows} == {"jav3"}
