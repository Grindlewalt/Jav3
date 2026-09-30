"""RUNS-08: the loop's per-turn counters are recorded and summed by /api/logs/calls."""
from backend import turnstats
from backend.db import get_db, init_db
from tests.test_tools import client  # noqa: F401


def _ev(**kw):
    return {"type": "turn_stats", "rounds": 4, "dsml_recovered": 1, "markup_retries": 0,
            "forced_conclusion": 0, "cap_hit": 0, "evictions": 3, "rereads": 1,
            "stop": "final", **kw}


async def test_record_and_summarise(tmp_env):
    await init_db()
    await turnstats.record(7, "op-1", _ev())
    await turnstats.record(7, "op-2", _ev(stop="cap", cap_hit=1, forced_conclusion=1))
    await turnstats.record(9, "op-3", _ev(stop="bogus", rounds="x", evictions=-5))
    db = await get_db()
    try:
        allc = await turnstats.summary(db, "-1 hours")
        one = await turnstats.summary(db, "-1 hours", conversation_id=7)
    finally:
        await db.close()
    assert allc["turns"] == 3 and one["turns"] == 2
    assert one["rounds"] == 8 and one["evictions"] == 6 and one["cap_hit"] == 1
    assert one["forced_conclusion"] == 1 and one["by_stop"] == {"final": 1, "cap": 1}
    # junk from the guest never breaks the row: bad numbers read 0, bad stop = final
    assert allc["by_stop"]["final"] == 2 and allc["evictions"] == 6


async def test_calls_endpoint_carries_the_turn_counters(client):  # noqa: F811
    await turnstats.record(5, "op-9", _ev(rereads=2))
    r = await client.get("/api/logs/calls?hours=1")
    assert r.status_code == 200, r.text
    turns = r.json()["turns"]
    assert turns["turns"] == 1 and turns["rereads"] == 2 and turns["dsml_recovered"] == 1
    r = await client.get("/api/logs/calls?hours=1&conversation_id=6")
    assert r.json()["turns"]["turns"] == 0
