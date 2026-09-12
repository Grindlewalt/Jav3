"""Vision in the ReAct loop: a tool can return a screenshot (via imageresult),
and run_turn attaches it to the model as an image block in a following user
message, keeps only the most recent one, and never lets the bytes leak into the
tool ledger or the tool message text."""
import struct
import zlib

import pytest

from backend.agent import imageresult, loop as loop_mod
from backend.agent.tools import registry
from backend.config import settings
from backend.db import get_db, init_db


def _png(path, w=8, h=8, rgb=(200, 30, 30)):
    def ch(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(
            ">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    png = (b"\x89PNG\r\n\x1a\n"
           + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b""))
    path.write_bytes(png)
    return str(path)


# --- pure helpers ------------------------------------------------------------

def test_image_message_reads_png(tmp_path):
    msg = loop_mod._image_message(_png(tmp_path / "s.png"))
    assert msg["role"] == "user" and isinstance(msg["content"], list)
    img = [p for p in msg["content"] if p["type"] == "image_url"][0]
    assert img["image_url"]["url"].startswith("data:image/png;base64,")


def test_image_message_missing_or_oversized_is_none(tmp_path, monkeypatch):
    assert loop_mod._image_message(str(tmp_path / "nope.png")) is None
    big = tmp_path / "big.png"
    big.write_bytes(b"x" * 100)
    monkeypatch.setattr(loop_mod, "_IMG_CAP", 10)
    assert loop_mod._image_message(str(big)) is None


def test_evict_stale_images_keeps_most_recent(monkeypatch):
    monkeypatch.setattr(settings, "screenshot_keep_recent", 1)
    msgs = [{"role": "user", "content": [{"type": "image_url",
             "image_url": {"url": "data:image/png;base64,A"}}]} for _ in range(3)]
    img_msgs = [{"idx": i, "round": i} for i in range(3)]
    loop_mod._evict_stale_images(msgs, img_msgs)
    # the last stays an image block; the first two become text stubs
    assert isinstance(msgs[2]["content"], list)
    assert all(isinstance(msgs[i]["content"], str)
               and "dropped" in msgs[i]["content"] for i in (0, 1))
    assert img_msgs[0]["evicted"] and img_msgs[1]["evicted"]


# --- end-to-end through run_turn --------------------------------------------

def _kind(content):
    if isinstance(content, list):
        return "image" if any(isinstance(p, dict) and p.get("type") == "image_url"
                              for p in content) else "listtext"
    if isinstance(content, str) and "earlier screenshot was dropped" in content:
        return "stub"
    return "text"


class _ScriptedModel:
    """Calls `screenshot` for `shots` rounds, then answers. Snapshots the
    (role, kind) of every message it is handed, immune to later eviction."""
    def __init__(self, shots):
        self.shots = shots
        self.call = 0
        self.seen = []

    async def complete(self, messages, tools=None, **kw):
        self.seen.append([(m["role"], _kind(m.get("content"))) for m in messages])
        if self.call < self.shots:
            tc = [{"id": f"c{self.call}", "type": "function",
                   "function": {"name": "screenshot", "arguments": "{}"}}]
            self.call += 1
            yield {"type": "message", "content": "", "tool_calls": tc, "usage": None}
        else:
            yield {"type": "message", "content": "done", "tool_calls": [], "usage": None}


async def test_screenshot_flows_to_model_and_old_ones_evict(tmp_path, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "screenshot_keep_recent", 1)
    png = _png(tmp_path / "shot.png")
    model = _ScriptedModel(shots=2)

    async def dispatch(name, args):
        assert name == "screenshot"
        return imageresult.with_image("screenshot captured", png)

    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())
    loop_mod._files_seen.clear()

    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.commit()
        events = []
        async for ev in loop_mod.run_turn(
                cid, "system", [{"role": "user", "content": "look"}],
                tools=[{"type": "function",
                        "function": {"name": "screenshot", "parameters": {}}}],
                on_tool_call=loop_mod.db_tool_sink(db, cid)):
            events.append(ev)
    finally:
        await db.close()

    # finished cleanly
    assert events[-1]["type"] == "final" and events[-1]["content"] == "done"

    # round 2 (index 1): the first screenshot reached the model as an image
    assert ("user", "image") in model.seen[1]
    # round 3 (index 2): exactly ONE live image (the latest), older one stubbed
    last = model.seen[2]
    assert sum(1 for r, k in last if k == "image") == 1
    assert ("user", "stub") in last

    # the tool message the model saw is clean text, not the marker/bytes
    assert imageresult.MARKER not in str(model.seen)
    assert ("tool", "text") in model.seen[1]

    # the tool_result events the GUI renders carry only the text, no image bytes
    tr = [e for e in events if e["type"] == "tool_result"]
    assert tr and all("data:image" not in e["result"] for e in tr)
    assert all(imageresult.MARKER not in e["result"] for e in tr)
