"""A deleted chat's usage must not follow its id (V3, live on the Pi,
2026-09-29): conversation ids are reused, and a new chat showed 9 calls and
their cost from a chat deleted the day before."""
from backend import chat, ctxstore
from backend.db import get_db, init_db, _detach_orphan_usage


async def _q(sql, args=()):
    db = await get_db()
    try:
        async with db.execute(sql, args) as cur:
            return [tuple(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _new_chat():
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.execute("INSERT INTO model_calls (conversation_id, model, input_tokens, "
                         "output_tokens, cache_hit, cache_miss) VALUES (?, 'm', 10, 1, 0, 10)",
                         (cid,))
        await db.execute("INSERT INTO turn_stats (conversation_id) VALUES (?)", (cid,))
        await db.commit()
        return cid
    finally:
        await db.close()


async def test_deleting_a_chat_detaches_its_usage(tmp_env):
    await init_db()
    cid = await _new_chat()
    ctxstore._heads[ctxstore._hkey(cid)] = ["a head"]
    await chat.delete_conversation(cid)
    assert await _q("SELECT conversation_id FROM model_calls") == [(None,)]
    assert await _q("SELECT conversation_id FROM turn_stats") == [(None,)]
    assert ctxstore._hkey(cid) not in ctxstore._heads
    # the id comes back for the next chat, with none of the old usage
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('new')")
        again = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    assert again == cid
    assert await _q("SELECT COUNT(*) FROM model_calls WHERE conversation_id = ?",
                    (again,)) == [(0,)]


async def test_rows_orphaned_before_the_fix_are_detached(tmp_env):
    await init_db()
    keep = await _new_chat()
    db = await get_db()
    try:
        await db.execute("INSERT INTO model_calls (conversation_id, model, input_tokens, "
                         "output_tokens, cache_hit, cache_miss) VALUES (999, 'm', 1, 1, 0, 1)")
        await db.commit()
        await _detach_orphan_usage(db)
        await db.commit()
    finally:
        await db.close()
    rows = await _q("SELECT conversation_id FROM model_calls ORDER BY id")
    assert rows == [(keep,), (None,)]
