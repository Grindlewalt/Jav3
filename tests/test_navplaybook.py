"""The navigation playbook rides its tool section (the desk / browser tools):
it stays short, names the merged tools' actions, and reaches the model through
the loop when the section loads (tests/test_tool_sections.py), never through
the host's assembled prompt."""
import asyncio
import contextlib

import httpx
import pytest

from backend import navplaybook
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


def _words(s):
    return len(s.split())


def test_word_budget():
    assert 80 < _words(navplaybook.desk_block()) <= 230
    assert "never use shell or other tools to change the computer's state" \
        in navplaybook.desk_block()
    assert 80 < _words(navplaybook.browser_block()) <= 220
    for block in (navplaybook.desk_block(), navplaybook.browser_block()):
        assert "#" not in block  # plain prose, no markdown headers


def test_blocks_follow_offered_tools():
    desk, browser = navplaybook.desk_block(), navplaybook.browser_block()
    assert navplaybook.for_tools([]) == ""
    assert navplaybook.for_tools(["read_file", "web_read"]) == ""
    only_desk = navplaybook.for_tools(["read_file", "desk_click"])
    assert desk in only_desk and browser not in only_desk
    only_browser = navplaybook.for_tools(iter(["browser_read_page"]))
    assert browser in only_browser and desk not in only_browser
    both = navplaybook.for_tools(["desk_screenshot", "browser_click"])
    assert desk in both and browser in both


def test_key_guidance_present():
    desk = navplaybook.desk_block()
    for s in ('desk(action="screenshot")', "element=", "target=", "region", "changed:",
              'action="key"', 'desk(action="wait"', "untrusted"):
        assert s in desk, s
    browser = navplaybook.browser_block()
    for s in ('action="read"', 'action="select"', 'action="key"',
              'action="hover"', 'action="screenshot"', "stale", "changed:",
              "secrets store", "untrusted"):
        assert s in browser, s


def test_append_to():
    specs = [{"type": "function", "function": {"name": "desk_click"}}]
    out = navplaybook.append_to("BASE", specs)
    assert out.startswith("BASE\n\n") and navplaybook.desk_block() in out
    plain = [{"type": "function", "function": {"name": "read_file"}}]
    assert navplaybook.append_to("BASE", plain) == "BASE"


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
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


def _capture(seen):
    async def turn(cid, system_prompt, history, *, tool_specs=None, **kw):
        seen["system_prompt"] = system_prompt
        seen["tools"] = [t["function"]["name"] for t in tool_specs or []]
        yield {"type": "final", "content": "ok"}
    return turn


async def _turn(client, monkeypatch, *, desk_on):
    from backend import browser, chat as chat_mod, desk
    seen = {}
    monkeypatch.setattr(chat_mod, "guest_turn", _capture(seen))
    monkeypatch.setattr(desk, "offered", lambda: desk_on)
    monkeypatch.setattr(browser, "offered", lambda: False)
    r = await client.post("/api/chat", json={"message": "hi", "confirm_peak": True})
    assert r.status_code == 200
    for _ in range(200):
        if "system_prompt" in seen:
            break
        await asyncio.sleep(0.01)
    for task in list(chat_mod._active_turns.values()):
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return seen


async def test_desk_tools_granted_only_when_offered_playbook_left_to_the_loop(
        client, monkeypatch):
    seen = await _turn(client, monkeypatch, desk_on=True)
    assert any(n.startswith("desk_") for n in seen["tools"])
    # the loop shows it with the desk section (a "hi" turn does not load it)
    assert navplaybook.desk_block() not in seen["system_prompt"]
    assert navplaybook.browser_block() not in seen["system_prompt"]

    seen = await _turn(client, monkeypatch, desk_on=False)
    assert not any(n.startswith("desk_") for n in seen["tools"])
    assert navplaybook.desk_block() not in seen["system_prompt"]
