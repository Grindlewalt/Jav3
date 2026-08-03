from datetime import datetime

from backend.agent.model import in_peak_window

WINDOWS = ["18:00-21:00", "23:00-03:00"]


def at(hour, minute=0):
    return datetime(2026, 7, 3, hour, minute)


def test_inside_evening_window():
    assert in_peak_window(at(18, 0), WINDOWS)
    assert in_peak_window(at(20, 59), WINDOWS)


def test_outside_windows():
    assert not in_peak_window(at(17, 59), WINDOWS)
    assert not in_peak_window(at(21, 0), WINDOWS)
    assert not in_peak_window(at(12, 0), WINDOWS)


def test_midnight_crossing_window():
    assert in_peak_window(at(23, 0), WINDOWS)
    assert in_peak_window(at(0, 30), WINDOWS)
    assert in_peak_window(at(2, 59), WINDOWS)
    assert not in_peak_window(at(3, 0), WINDOWS)
    assert not in_peak_window(at(22, 59), WINDOWS)


def test_confirmation_flow(tmp_env):
    from backend.agent.model import confirm_peak, peak_confirmed

    assert not peak_confirmed(12345)
    confirm_peak(12345)
    assert peak_confirmed(12345)


# --- the peak gate must not orphan conversations (convo-36 post-mortem) --------
# The old order created the conversation row, THEN 409'd for peak confirmation;
# the retry opened a fresh conversation, leaving a blank twin in the sidebar.

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
        yield c


async def _count(table):
    db = await get_db()
    try:
        async with db.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:
            return (await cur.fetchone())["n"]
    finally:
        await db.close()


async def test_peak_409_on_new_chat_leaves_no_orphan(client, monkeypatch):
    from backend import chat as chat_mod
    monkeypatch.setattr(chat_mod, "in_peak_window", lambda *a, **k: True)

    async def stub_turn(db, cid, system_prompt, history, tools=None, **kw):
        yield {"type": "final", "content": "ok"}
    monkeypatch.setattr(chat_mod, "guest_turn", stub_turn)

    r = await client.post("/api/chat", json={"message": "hello"})
    assert r.status_code == 409
    assert r.json()["detail"] == "peak_confirmation_required"
    assert await _count("conversations") == 0   # no orphan row

    # the confirmed retry creates exactly one conversation and runs the turn
    r = await client.post("/api/chat",
                          json={"message": "hello", "confirm_peak": True})
    assert r.status_code == 200
    assert await _count("conversations") == 1


async def test_peak_409_on_existing_chat_keeps_id_and_state(client, monkeypatch):
    from backend import chat as chat_mod
    from backend.agent import model as model_mod
    # confirmations are keyed by conversation id in module state; a prior
    # test's tmp DB reused id 1, so start clean
    model_mod._peak_confirmations.clear()

    async def stub_turn(db, cid, system_prompt, history, tools=None, **kw):
        yield {"type": "final", "content": "ok"}
    monkeypatch.setattr(chat_mod, "guest_turn", stub_turn)

    # first exchange outside peak creates the conversation
    monkeypatch.setattr(chat_mod, "in_peak_window", lambda *a, **k: False)
    r = await client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 200
    db = await get_db()
    try:
        async with db.execute("SELECT MAX(id) AS m FROM conversations") as cur:
            cid = (await cur.fetchone())["m"]
    finally:
        await db.close()
    msgs_before = await _count("messages")

    # peak turns on: an unconfirmed follow-up 409s with the id and no side effects
    monkeypatch.setattr(chat_mod, "in_peak_window", lambda *a, **k: True)
    r = await client.post("/api/chat",
                          json={"message": "more", "conversation_id": cid})
    assert r.status_code == 409
    assert r.headers.get("X-Conversation-Id") == str(cid)
    assert await _count("conversations") == 1
    assert await _count("messages") == msgs_before
