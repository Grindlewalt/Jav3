"""Wake-word standby, the double-clap fast path, and queue plumbing —
session-level with the usual fake transports; no model, no audio."""
import asyncio

import pytest

from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.memory import ensure_memory_seeds

from tests.test_voice_session import (first_tts, make_session, scripted_turn,
                                      settle)


@pytest.fixture
async def seeded(tmp_env):
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


async def mark_greeted():
    """Most tests aren't about the startup greeting — pre-spend it."""
    from datetime import datetime
    from backend.db import set_state
    from backend.voice import GREETING_KEY
    db = await get_db()
    try:
        await set_state(db, GREETING_KEY, datetime.now().strftime("%Y-%m-%d"))
    finally:
        await db.close()


async def test_asleep_until_wake_word(seeded, monkeypatch):
    from backend import chat as chat_mod
    await mark_greeted()
    session, out = make_session(monkeypatch)
    monkeypatch.setattr(chat_mod, "run_turn",
                        scripted_turn("Good morning, sir. "))

    # sidecar announces a wake model → session starts on standby
    await session._on_sidecar_json(
        {"type": "ready", "stt": "small", "tts": "k", "wake": "hey_jarvis_v0.1"})
    assert session.state == "asleep"

    # words while asleep are ignored — no turn, nothing persisted
    await session._on_transcript("what time is it")
    assert session.turn_task is None and session.cid is None

    # the wake word brings him up: chime to the browser, back to listening
    await session._on_sidecar_json({"type": "wake"})
    assert session.state == "listening"
    assert any(isinstance(m, dict) and m.get("type") == "wake" for m in out)

    # and now speech works
    await session._on_transcript("what time is it")
    await settle(session)
    assert session.cid is not None


async def test_dozes_off_after_idle_timeout(seeded, monkeypatch):
    from backend import voice
    await mark_greeted()
    monkeypatch.setattr(voice.settings, "voice_wake_timeout", 0.05)
    session, out = make_session(monkeypatch)
    await session._on_sidecar_json({"type": "ready", "wake": "hey_jarvis_v0.1"})
    await session._on_sidecar_json({"type": "wake"})
    assert session.state == "listening"
    await asyncio.sleep(0.15)
    assert session.state == "asleep"


async def test_no_wake_model_means_always_listening(seeded, monkeypatch):
    await mark_greeted()
    session, out = make_session(monkeypatch)
    await session._on_sidecar_json({"type": "ready", "wake": None})
    assert session.state == "listening"
    assert session._sleep_task is None


async def test_first_wake_of_the_day_greets_with_briefing(seeded, monkeypatch):
    from backend import chat as chat_mod
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO schedules (name, kind, task, cadence_kind, daily_at, "
            "next_run, last_run, last_result) VALUES ('Morning News', 'agent', "
            "'fetch news', 'daily', '06:00', datetime('now', '+1 day'), "
            "datetime('now', '-1 hour'), 'Rain later today; the Mars rover "
            "found something shiny.')")
        await db.commit()
    finally:
        await db.close()

    session, out = make_session(monkeypatch)
    seen = {}

    async def turn(cid, system_prompt, history, tools=None, **kw):
        seen["user"] = history[-1]["content"]
        yield {"type": "final", "content": "Morning, sir. Rain later; the "
               "rover found something shiny. "}

    monkeypatch.setattr(chat_mod, "run_turn", turn)
    await session._on_sidecar_json({"type": "ready", "wake": "hey_jarvis_v0.1"})
    assert session.state == "asleep"
    await session._on_sidecar_json({"type": "wake"})
    await settle(session)

    assert "[startup" in seen["user"]
    assert "Morning News" in seen["user"]
    assert "something shiny" in seen["user"]

    # play out the greeting so the session can settle back to sleep
    for t in await first_tts(session):
        await session._on_sidecar_json(
            {"type": "tts_done", "id": t["id"], "dur_ms": 500})
        await session.on_browser_json(
            {"type": "chunk_played", "chunk_id": t["id"]})
    assert session.state == "listening"

    # same day, second wake: no second greeting turn
    seen.clear()
    await session._sleep()
    await session._on_sidecar_json({"type": "wake"})
    assert seen == {} and session.state == "listening"


async def test_double_clap_dispatches_music_directly(seeded, monkeypatch):
    from backend import voice
    session, out = make_session(monkeypatch)
    calls = []

    async def fake_dispatch(name, args):
        calls.append((name, args))
        return "playing Kickstart My Heart in the Jarvis player on Mac."

    monkeypatch.setattr(voice, "tool_dispatch", fake_dispatch)
    await session.on_browser_json({"type": "double_clap"})
    for _ in range(100):
        await asyncio.sleep(0.01)
        if calls:
            break

    assert calls and calls[0][0] == "music_play"
    assert calls[0][1]["query"] in voice.CLAP_TRACKS
    assert calls[0][1]["where"] == "jarvis"
    # no turn, no conversation — the model was never involved
    assert session.turn_task is None and session.cid is None
    clap = [m for m in out if isinstance(m, dict) and m.get("type") == "clap"]
    assert clap and "playing" in clap[0]["result"]


async def test_double_clap_fires_once_per_session(seeded, monkeypatch):
    from backend import voice
    session, out = make_session(monkeypatch)
    calls = []

    async def fake_dispatch(name, args):
        calls.append((name, args))
        return "playing."

    monkeypatch.setattr(voice, "tool_dispatch", fake_dispatch)
    await session.on_browser_json({"type": "double_clap"})
    for _ in range(100):
        await asyncio.sleep(0.01)
        if calls:
            break
    assert len(calls) == 1

    # the detector misfires again mid-session — nothing happens
    await session.on_browser_json({"type": "double_clap"})
    await asyncio.sleep(0.1)
    assert len(calls) == 1


async def test_clap_tracks_tool_edits_the_list_live(seeded, monkeypatch):
    """The tool edits, the session reads: add + remove in one call, forgiving
    spelling on remove, and the very next clap uses the edited list."""
    import tools.clap_tracks.handler as h
    from backend import voice

    async def no_library(titles):
        return []

    monkeypatch.setattr(h, "_library_check", no_library)
    result = await h.run(add=["Thunderstruck"],
                         remove=["should i stay or should i go"])
    assert "added: Thunderstruck" in result
    assert "removed: Should I Stay or Should I Go" in result
    assert "Kickstart My Heart; Thunderstruck" in result

    db = await get_db()
    try:
        tracks = await voice.get_clap_tracks(db)
    finally:
        await db.close()
    assert tracks == ["Kickstart My Heart", "Thunderstruck"]

    session, out = make_session(monkeypatch)
    calls = []

    async def fake_dispatch(name, args):
        calls.append(args)
        return "playing."

    monkeypatch.setattr(voice, "tool_dispatch", fake_dispatch)
    await session.on_browser_json({"type": "double_clap"})
    for _ in range(100):
        await asyncio.sleep(0.01)
        if calls:
            break
    assert calls and calls[0]["query"] in tracks


async def test_empty_clap_list_disables_the_gesture(seeded, monkeypatch):
    import tools.clap_tracks.handler as h
    from backend import voice

    result = await h.run(remove=["kickstart", "should i stay"])
    assert "EMPTY" in result

    session, out = make_session(monkeypatch)
    calls = []

    async def fake_dispatch(name, args):
        calls.append(args)
        return "playing."

    monkeypatch.setattr(voice, "tool_dispatch", fake_dispatch)
    await session.on_browser_json({"type": "double_clap"})
    await asyncio.sleep(0.1)
    assert not calls
    clap = [m for m in out if isinstance(m, dict) and m.get("type") == "clap"]
    assert clap and "empty" in clap[0]["result"]


async def test_music_play_queue_param_appends(tmp_env, monkeypatch):
    """The tool pushes 'queue_add' (never 'play') when queue=true, and the
    reply says queued — the current track must not be interrupted."""
    import tools.music_play.handler as h

    async def fake_queue_rows(ids):
        return [{"id": i, "title": f"t{i}", "artist": "", "album": "",
                 "duration": 100, "src": f"/api/x/{i}"} for i in ids]

    pushes = []
    monkeypatch.setattr(h, "_queue_rows", fake_queue_rows)
    monkeypatch.setattr(h.gui, "resolve_tab", lambda w, a: ("tab1", "Mac"))
    monkeypatch.setattr(h.gui, "player_push",
                        lambda action, tab=None, **f: pushes.append(
                            (action, f)) or 1)

    result = await h.run(ids=[5, 6], queue=True, where="jarvis")
    assert pushes and pushes[0][0] == "queue_add"
    assert len(pushes[0][1]["queue"]) == 2
    assert "queued" in result and "2 track(s)" in result

    result = await h.run(ids=[7], queue=True, where="app")
    assert "only the Jarvis player has a queue" in result
