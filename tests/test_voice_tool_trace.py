"""The local voice tier's context: past turns' tool work replayed into the
model-facing history, the music library in the prompt, and the tier switch.

Why the replay exists, measured against the live tier with the production
prompt: qwen3.5:4b called a tool on "play some Zach Bryan" 6/6 times with no
history and 0/6 after two prose-only exchanges — a history where every past
action reads as a bare sentence teaches a small model that talking IS acting.
Replaying the same exchanges with their tool turns took it to 12/12.
"""
import json

import pytest

from backend import chat, compaction
from backend.db import get_db, init_db
from backend.voice_text import library_block


@pytest.fixture
async def conv(tmp_env):
    await init_db()
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO conversations (summary) VALUES ('voice')")
        cid = cur.lastrowid
        await db.commit()
        yield cid, db
    finally:
        await db.close()


async def _exchange(db, cid, user, reply, calls=()):
    """One persisted turn, exactly as chat.py writes it: user row, tool_calls
    during the turn, assistant row, then the linkage."""
    await db.execute("INSERT INTO messages (conversation_id, role, content) "
                     "VALUES (?, 'user', ?)", (cid, user))
    async with db.execute("SELECT COALESCE(MAX(id), 0) m FROM tool_calls "
                          "WHERE conversation_id = ?", (cid,)) as cur:
        before = (await cur.fetchone())["m"]
    for tool, args, result in calls:
        await db.execute(
            "INSERT INTO tool_calls (conversation_id, tool, args, result) "
            "VALUES (?, ?, ?, ?)", (cid, tool, json.dumps(args), result))
    cur = await db.execute(
        "INSERT INTO messages (conversation_id, role, content) "
        "VALUES (?, 'assistant', ?)", (cid, reply))
    await chat._link_tool_calls(db, cid, before, cur.lastrowid)
    await db.commit()


async def test_history_without_trace_shows_prose_only(conv):
    cid, db = conv
    await _exchange(db, cid, "play something fast", 'Playing "Song 2" now.',
                    [("music_play", {"tag": "fast"}, "playing Song 2 — Blur.")])

    history = await compaction.assemble(db, cid, "sys")

    assert [m["role"] for m in history] == ["user", "assistant"]
    assert all("tool_calls" not in m for m in history)


async def test_tool_trace_replays_the_call_and_its_result(conv):
    cid, db = conv
    await _exchange(db, cid, "play something fast", 'Playing "Song 2" now.',
                    [("music_play", {"tag": "fast"}, "playing Song 2 — Blur.")])
    await _exchange(db, cid, "turn it down", "Lowering it.",
                    [("computer_volume", {"action": "set", "percent": 30},
                      "macbook volume set to 30%.")])

    history = await compaction.assemble(db, cid, "sys", tool_trace=200)

    assert [m["role"] for m in history] == [
        "user", "assistant", "tool", "assistant",
        "user", "assistant", "tool", "assistant"]
    call = history[1]["tool_calls"][0]
    assert call["function"]["name"] == "music_play"
    assert json.loads(call["function"]["arguments"]) == {"tag": "fast"}
    # the tool result answers the id the assistant turn just claimed
    assert history[2]["tool_call_id"] == call["id"]
    assert history[2]["content"] == "playing Song 2 — Blur."
    # the spoken reply still follows its own tool work
    assert history[3] == {"role": "assistant", "content": 'Playing "Song 2" now.'}


async def test_tool_results_are_truncated_to_the_cap(conv):
    cid, db = conv
    await _exchange(db, cid, "what's in the library", "Thirty tracks.",
                    [("music_search", {"query": ""}, "x" * 5000)])

    history = await compaction.assemble(db, cid, "sys", tool_trace=200)

    assert len(history[2]["content"]) == 200


async def test_a_prose_only_turn_replays_unchanged(conv):
    """A turn that genuinely called nothing must not grow a phantom trace."""
    cid, db = conv
    await _exchange(db, cid, "what model are you", "The local one.")

    history = await compaction.assemble(db, cid, "sys", tool_trace=200)

    assert history == [{"role": "user", "content": "what model are you"},
                       {"role": "assistant", "content": "The local one."}]


async def test_rows_from_before_the_linkage_column_are_skipped(conv):
    """message_id is NULL for every tool_call written before the migration;
    those replay as prose rather than being attached to the wrong turn."""
    cid, db = conv
    await db.execute("INSERT INTO messages (conversation_id, role, content) "
                     "VALUES (?, 'user', 'old request')", (cid,))
    await db.execute(
        "INSERT INTO tool_calls (conversation_id, tool, args, result) "
        "VALUES (?, 'music_play', '{}', 'played')", (cid,))
    await db.execute("INSERT INTO messages (conversation_id, role, content) "
                     "VALUES (?, 'assistant', 'Done.')", (cid,))
    await db.commit()

    history = await compaction.assemble(db, cid, "sys", tool_trace=200)

    assert [m["role"] for m in history] == ["user", "assistant"]


async def test_interrupted_turn_keeps_the_tools_it_already_ran(conv):
    """A barge-in cancels the turn, but the song it started really is playing —
    the marker turn carries the trace so the next turn still sees tool use."""
    cid, db = conv
    await _exchange(db, cid, "play something", chat.INTERRUPTED_MARKER + " x",
                    [("music_play", {"query": "anything"}, "playing.")])

    history = await compaction.assemble(db, cid, "sys", tool_trace=200)

    assert history[1]["tool_calls"][0]["function"]["name"] == "music_play"


# --- the library block ------------------------------------------------------

def test_library_block_is_stable_and_names_real_ids():
    tracks = [{"id": 30, "title": "Song 2", "artist": "Blur"},
              {"id": 27, "title": "Many Men", "artist": "50 Cent", "tag": "fast"}]

    block = library_block(tracks)

    assert block.index("[27]") < block.index("[30]")     # id order, not input
    assert "[27] Many Men — 50 Cent #fast" in block
    assert library_block(list(reversed(tracks))) == block   # byte-stable prefix
    assert library_block([]) == ""


def test_library_block_survives_a_track_with_only_a_title():
    assert "[4] Untitled Demo" in library_block([{"id": 4, "title": "Untitled Demo"}])


# --- the local tier's window ------------------------------------------------

def test_local_tier_compacts_against_its_own_window():
    """The local tier runs in llama.cpp's 16k slot, not DeepSeek's 1M. Sizing
    it against the global default means compaction never fires and the prompt
    silently loses its front — tool definitions first."""
    from backend.config import settings
    local = (settings.voice_local_context_window
             - settings.voice_local_max_tokens - 5_000)
    # comfortably over the local tier's window, still far under the global one,
    # derived from the setting so raising -c does not silently void this test
    history = [{"role": "user", "content": "x" * (local * 8)}]

    assert not compaction.needs_compaction("sys", history, None)
    assert compaction.needs_compaction("sys", history, None, window=local)


def test_explicit_window_overrides_the_global_budget():
    from backend import compaction as c
    assert c.effective_window(4_242) == 4_242
    assert c.effective_window() == c.effective_window(None)
