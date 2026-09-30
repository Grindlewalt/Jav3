"""FX1 / ROBUST-06 (the host's halves, in guest_turn and workspace_xfer): the guest
is the hostile side, so what it sends is read within bounds.

A line with no newline used to be concatenated chunk by chunk (quadratic) with no
cap (16 MB cost 550 MB and seconds); a 0.9 MB gzip in `staged` unpacked to a
200 MB file and 730 MB of host memory."""
import asyncio
import io
import socket
import tarfile

import pytest

from backend.config import settings
from backend.db import get_db, init_db
from backend.vm import guest_turn as gt
from backend.vm import workspace_xfer as wx

from tests.test_fx1_guest_turn import _books_clear, _drive


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
async def env(tmp_env):
    await init_db()
    (settings.projects_dir / "lim").mkdir(parents=True)
    gt._ws_holds.pop("fx1", None)
    yield tmp_env
    gt._ws_holds.pop("fx1", None)
    gt.broker.release_token("op-fx1")
    gt.broker.release_turn("op-fx1")
    gt.budget_mod.release("op-fx1")


# --- reading a line --------------------------------------------------------------

async def _pair():
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    return a, b, asyncio.get_running_loop()


async def test_a_long_line_arrives_whole_and_the_rest_stays_buffered():
    a, b, loop = await _pair()
    big = b"x" * 3_000_000
    sender = asyncio.create_task(loop.sock_sendall(b, big + b"\nsecond\nthi"))
    buf = bytearray()
    assert await gt._recv_line(loop, a, buf, 10_000_000) == big
    await sender
    assert await gt._recv_line(loop, a, buf, 10_000_000) == b"second"
    b.close()
    assert await gt._recv_line(loop, a, buf, 10_000_000) is None     # EOF mid-line
    a.close()


async def test_a_line_over_the_limit_is_an_error_not_a_buffer():
    a, b, loop = await _pair()
    sender = asyncio.create_task(loop.sock_sendall(b, b"y" * 200_000))
    with pytest.raises(gt.GuestStreamError, match="line of over"):
        await asyncio.wait_for(gt._recv_line(loop, a, bytearray(), 50_000), 10)
    sender.cancel()
    a.close()
    b.close()


async def test_a_quiet_guest_times_out_when_asked_to():
    a, b, loop = await _pair()
    assert await gt._recv_line(loop, a, bytearray(), 1000, timeout=0.05) is None
    a.close()
    b.close()


async def test_a_turn_whose_guest_floods_a_line_fails_cleanly(env, monkeypatch):
    monkeypatch.setattr(gt, "MAX_LINE", 20_000)
    (settings.projects_dir / "fx1").mkdir(parents=True)

    async def flood(loop, sock, spec):
        await loop.sock_sendall(sock, b"z" * 100_000)        # never a newline

    async def nothing(spec, box):
        return None
    monkeypatch.setattr(gt, "_pinned_rpc", nothing)
    with pytest.raises(gt.GuestStreamError, match="line of over"):
        await _drive(monkeypatch, flood)
    assert all(_books_clear().values()), _books_clear()


# --- unpacking the buffer --------------------------------------------------------

async def _events(kind):
    db = await get_db()
    try:
        async with db.execute("SELECT summary FROM security_events WHERE kind = ?",
                              (kind,)) as cur:
            return [r["summary"] for r in await cur.fetchall()]
    finally:
        await db.close()


async def test_a_bomb_is_refused_by_its_header_and_the_rest_still_lands(env, monkeypatch):
    monkeypatch.setattr(wx, "MAX_MEMBER_BYTES", 100_000)
    bomb = b"\0" * 20_000_000                     # ~20 KB once gzipped
    tar = _tar({"ok.txt": b"fine\n", "bomb.bin": bomb, "also.txt": b"also\n"})
    assert len(tar) < 100_000
    res = await wx.apply_guest_writes("lim", tar)
    assert res["applied"] == ["ok.txt", "also.txt"]
    assert "larger than" in res["refused"]["bomb.bin"]
    d = settings.projects_dir / "lim"
    assert (d / "ok.txt").is_file() and not (d / "bomb.bin").exists()
    assert "bomb.bin" in wx.describe_unapplied(res)
    assert any("bomb.bin" in s for s in await _events("write_flag"))    # the operator's record


async def test_the_whole_buffer_is_bounded_too(env, monkeypatch):
    monkeypatch.setattr(wx, "MAX_BUFFER_BYTES", 2_500)
    res = await wx.apply_guest_writes("lim", _tar(
        {f"f{i}.txt": b"a" * 1_000 for i in range(4)}))
    assert res["applied"] == ["f0.txt", "f1.txt"]
    assert set(res["refused"]) == {"f2.txt", "f3.txt"}


async def test_too_many_files_stops_the_reading(env, monkeypatch):
    monkeypatch.setattr(wx, "MAX_MEMBERS", 3)
    res = await wx.apply_guest_writes("lim", _tar({f"f{i}.txt": b"x" for i in range(10)}))
    assert res["applied"] == ["f0.txt", "f1.txt", "f2.txt"]
    assert "more than 3 files" in res["refused"]["(the rest of the buffer)"]


async def test_a_declared_stream_over_the_limit_stops_before_unpacking(env, monkeypatch):
    monkeypatch.setattr(wx, "MAX_STREAM_BYTES", 10_000)
    monkeypatch.setattr(wx, "MAX_MEMBER_BYTES", 5_000)
    res = await wx.apply_guest_writes("lim", _tar(
        {"a.txt": b"a" * 3_000, "huge.bin": b"\0" * 50_000_000, "b.txt": b"b"}))
    assert res["applied"] == ["a.txt"]
    assert "in all" in res["refused"]["(the rest of the buffer)"]


async def test_a_buffer_that_breaks_partway_keeps_what_came_before(env):
    tar = _tar({"a.txt": b"a" * 1000, "b.txt": bytes(range(256)) * 2000})
    res = await wx.apply_guest_writes("lim", tar[:-200])       # cut off mid-stream
    assert "a.txt" in res["applied"]
    assert "damaged" in res["refused"]["(the rest of the buffer)"]
