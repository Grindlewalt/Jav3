"""FX1: the host side of a guest turn keeps its books straight and its promises.

ROBUST-01  a boot that fails must not leak the per-turn registrations
ROBUST-02  a turn that stops early still brings the guest's staged edits home
ROBUST-03  files refused or failed at reconcile are told to the operator and model
ROBUST-04  a stream that ends without `final` is an error, not a normal end

The guest is one end of a socketpair (as in test_always_loaded); a scenario is an
async function that plays the guest's side of one connection.
"""
import asyncio
import base64
import io
import json
import socket
import tarfile
import types

import pytest

from backend import egress
from backend.agent import budget as budget_mod
from backend.config import settings
from backend.db import get_db, init_db
from backend.vm import boxes, broker, guest_turn as gt


def _tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _staged(files: dict[str, str]) -> dict:
    return {"type": "staged", "tar_b64": base64.b64encode(_tar(files)).decode()}


async def _send(loop, sock, ev: dict) -> None:
    await loop.sock_sendall(sock, (json.dumps(ev) + "\n").encode())


class Ctl:
    """The box controller: counts pins so a leaked pin shows."""

    def __init__(self, fail: Exception | None = None):
        self.fail, self.acquired, self.released = fail, 0, 0

    async def acquire(self):
        if self.fail:
            raise self.fail
        self.acquired += 1

    def release(self):
        self.released += 1


def _wire(monkeypatch, ctl: Ctl | None = None):
    """Point guest_turn at a socketpair box. Returns (guest_end, ctl)."""
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    ctl = ctl or Ctl()

    async def connect(port):
        return a

    box = types.SimpleNamespace(is_shared=True, id="shared",
                                transport=types.SimpleNamespace(connect=connect))

    async def for_project(slug):
        return box

    async def wait_turn_slot(bx, slug):
        return bx

    monkeypatch.setattr(boxes, "for_project", for_project)
    monkeypatch.setattr(boxes, "wait_turn_slot", wait_turn_slot)
    monkeypatch.setattr(boxes, "controller", lambda bx: ctl)
    return b, ctl


async def _read_spec(loop, sock) -> dict:
    buf = b""
    while b"\n" not in buf:
        chunk = await loop.sock_recv(sock, 65536)
        if not chunk:
            break
        buf += chunk
    return json.loads(buf.split(b"\n", 1)[0])


def _env(op="op-fx1", project="fx1"):
    return broker.TurnEnvelope(op_id=op, web_session="ws", active_project=project,
                               conversation_id=7)


def _books_clear(op="op-fx1", slug="fx1"):
    """Everything a turn registers, and that must be gone when it ends."""
    return {
        "hold": slug not in gt._ws_holds,
        "budget": budget_mod.get(op) is None,
        "envelope": broker.get_turn(op) is None,
        "token": op not in broker._op_tokens,
        "egress": not any(e["op_id"] == op for e in egress._stack),
    }


@pytest.fixture
async def env(tmp_env, monkeypatch):
    await init_db()
    (settings.projects_dir / "fx1").mkdir(parents=True)
    (settings.projects_dir / "fx1" / "a.txt").write_text("original\n")
    gt._ws_holds.pop("fx1", None)
    yield tmp_env
    # a failing test must not leave its registrations for the next one
    gt._ws_holds.pop("fx1", None)
    broker.release_token("op-fx1")
    broker.release_turn("op-fx1")
    budget_mod.release("op-fx1")


async def _events(kind):
    db = await get_db()
    try:
        async with db.execute("SELECT severity, summary, detail FROM security_events "
                              "WHERE kind = ? ORDER BY id", (kind,)) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _drive(monkeypatch, scenario, *, ctl=None, **kw):
    """Run a guest_turn against `scenario(loop, sock, spec)`; returns
    (events, ctl). Any exception guest_turn raises propagates."""
    guest_sock, ctl = _wire(monkeypatch, ctl)
    loop = asyncio.get_running_loop()

    async def guest():
        spec = await _read_spec(loop, guest_sock)
        try:
            await scenario(loop, guest_sock, spec)
        finally:
            guest_sock.close()

    task = asyncio.create_task(guest())
    events = []

    async def consume():
        async for ev in gt.guest_turn(7, "sys", [], op_id="op-fx1", envelope=_env(),
                                      active_slug="fx1", push_workspace=True, **kw):
            events.append(ev)
    try:
        await asyncio.wait_for(consume(), 10)     # a hang is a failure, not a stall
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return events, ctl


# --- ROBUST-01 ------------------------------------------------------------------

async def test_a_failed_boot_leaks_nothing(env, monkeypatch):
    """guest_vm.acquire() raising (image not built, boot timeout, no KVM) used to
    leave the workspace hold, the budget, the envelope, the op token and the
    egress attribution registered, so every later turn on the project lost its
    workspace push."""
    async def never(loop, sock, spec):
        raise AssertionError("the guest was never reached")

    with pytest.raises(RuntimeError, match="no KVM"):
        await _drive(monkeypatch, never, ctl=Ctl(fail=RuntimeError("no KVM")))
    assert all(_books_clear().values()), _books_clear()


async def test_a_failed_tar_build_leaks_nothing_and_never_pins_the_guest(env, monkeypatch):
    def boom(slug):
        raise OSError("disk gone")
    monkeypatch.setattr(gt.workspace_xfer, "build_merged_tar", boom)

    async def never(loop, sock, spec):
        raise AssertionError("the guest was never reached")

    ctl = Ctl()
    with pytest.raises(OSError, match="disk gone"):
        await _drive(monkeypatch, never, ctl=ctl)
    assert all(_books_clear().values()), _books_clear()
    assert ctl.acquired == ctl.released          # a pin taken is a pin returned


async def test_the_turn_after_a_failed_boot_still_gets_the_workspace(env, monkeypatch):
    async def never(loop, sock, spec):
        raise AssertionError

    with pytest.raises(RuntimeError):
        await _drive(monkeypatch, never, ctl=Ctl(fail=RuntimeError("no KVM")))

    got = {}

    async def ok(loop, sock, spec):
        got["tar"] = bool(spec.get("workspace_tar_b64"))
        await _send(loop, sock, {"type": "final", "content": "hi"})
        await _send(loop, sock, _staged({}))

    events, _ = await _drive(monkeypatch, ok)
    assert got["tar"] is True                    # first-in again: the copy is pushed
    assert [e["type"] for e in events] == ["final"]


# --- ROBUST-04 ------------------------------------------------------------------

async def test_a_stream_that_ends_without_final_is_an_error(env, monkeypatch):
    """The guest OOMs or the VM is reaped mid-turn: the socket just closes. That
    used to look like a normal end and an empty assistant message was saved."""
    async def dies(loop, sock, spec):
        await _send(loop, sock, {"type": "token", "content": "par"})
        await _send(loop, sock, {"type": "token", "content": "tial"})

    got = []
    guest_sock, ctl = _wire(monkeypatch)
    loop = asyncio.get_running_loop()

    async def guest():
        spec = await _read_spec(loop, guest_sock)
        await dies(loop, guest_sock, spec)
        guest_sock.close()

    task = asyncio.create_task(guest())

    async def consume():
        async for ev in gt.guest_turn(7, "sys", [], op_id="op-fx1", envelope=_env(),
                                      active_slug="fx1", push_workspace=True):
            got.append(ev["type"])
    with pytest.raises(ConnectionError, match="mid-turn"):
        await asyncio.wait_for(consume(), 10)
    await task
    assert got == ["token", "token"]             # what streamed was still delivered
    assert all(_books_clear().values()), _books_clear()


async def test_a_stream_with_final_still_ends_normally(env, monkeypatch):
    async def fine(loop, sock, spec):
        await _send(loop, sock, {"type": "token", "content": "x"})
        await _send(loop, sock, {"type": "final", "content": "done"})
        await _send(loop, sock, _staged({}))

    events, _ = await _drive(monkeypatch, fine)
    assert [e["type"] for e in events] == ["token", "final"]


# --- ROBUST-02 ------------------------------------------------------------------

def _pull_returns(monkeypatch, files: dict[str, str] | None, *, calls=None, exc=None,
                  hang=False):
    """What the guest's `pull` RPC answers (the rescue path)."""
    async def rpc(spec, box):
        if calls is not None:
            calls.append(spec["mode"])
        if hang:
            await asyncio.sleep(60)
        if exc:
            raise exc
        return _staged(files) if files is not None else None
    monkeypatch.setattr(gt, "_pinned_rpc", rpc)


async def _stop_after_first_token(monkeypatch, ctl=None):
    """The consumer closes the generator mid-turn, as chat.py does on a stop."""
    guest_sock, ctl = _wire(monkeypatch, ctl)
    loop = asyncio.get_running_loop()
    parked = asyncio.Event()

    async def guest():
        await _read_spec(loop, guest_sock)
        await _send(loop, guest_sock, {"type": "token", "content": "working"})
        await parked.wait()                     # mid-turn until the host goes away
        guest_sock.close()

    task = asyncio.create_task(guest())
    gen = gt.guest_turn(7, "sys", [], op_id="op-fx1", envelope=_env(),
                        active_slug="fx1", push_workspace=True)
    first = await asyncio.wait_for(gen.__anext__(), 10)
    assert first["type"] == "token"
    await asyncio.wait_for(gen.aclose(), 10)
    parked.set()
    await task
    return ctl


async def test_a_stopped_turn_brings_its_staged_edits_home(env, monkeypatch):
    """The operator pressed stop after the agent wrote b.txt: the edit used to sit
    in the guest until the next turn's unpack wiped it, while the transcript said
    write_file ok."""
    calls = []
    _pull_returns(monkeypatch, {"b.txt": "written before the stop\n"}, calls=calls)
    ctl = await _stop_after_first_token(monkeypatch)
    assert calls == ["pull"]
    assert (settings.projects_dir / "fx1" / "b.txt").read_text() == "written before the stop\n"
    assert all(_books_clear().values()), _books_clear()
    assert ctl.acquired == ctl.released


async def test_a_stop_with_the_guest_gone_still_unwinds(env, monkeypatch):
    _pull_returns(monkeypatch, None, exc=ConnectionResetError("guest gone"))
    ctl = await _stop_after_first_token(monkeypatch)
    assert all(_books_clear().values()), _books_clear()
    assert ctl.acquired == ctl.released


async def test_a_stop_with_a_hung_guest_does_not_wedge(env, monkeypatch):
    monkeypatch.setattr(gt, "RESCUE_TIMEOUT", 0.2)
    _pull_returns(monkeypatch, None, hang=True)
    ctl = await _stop_after_first_token(monkeypatch)     # would block for a minute
    assert all(_books_clear().values()), _books_clear()
    assert ctl.acquired == ctl.released


async def test_a_dropped_stream_still_brings_its_edits_home(env, monkeypatch):
    calls = []
    _pull_returns(monkeypatch, {"c.txt": "kept\n"}, calls=calls)

    async def dies(loop, sock, spec):
        await _send(loop, sock, {"type": "token", "content": "x"})

    with pytest.raises(gt.GuestStreamError):
        await _drive(monkeypatch, dies)
    assert calls == ["pull"]
    assert (settings.projects_dir / "fx1" / "c.txt").read_text() == "kept\n"


async def test_a_final_with_no_staged_is_rescued_and_still_delivered(env, monkeypatch):
    calls = []
    _pull_returns(monkeypatch, {"d.txt": "late\n"}, calls=calls)

    async def no_staged(loop, sock, spec):
        await _send(loop, sock, {"type": "final", "content": "all done"})

    events, _ = await _drive(monkeypatch, no_staged)
    assert [e["type"] for e in events] == ["final"]
    assert events[0]["content"] == "all done"           # every file landed: no note
    assert calls == ["pull"]
    assert (settings.projects_dir / "fx1" / "d.txt").read_text() == "late\n"


async def test_a_guest_loop_crash_is_raised_not_answered(env, monkeypatch):
    """M2's note: when the model stream failed for good, the guest sent its
    crash as a `final` that read like an answer. It now carries `error`, and
    the host raises it after the edits came home."""
    from backend.vm import guest_turn as gt
    _pull_returns(monkeypatch, {})

    async def crashed(loop, sock, spec):
        await _send(loop, sock, {"type": "final", "content": "(guest loop error: X)",
                                 "error": "ModelError: stream dropped"})
        await _send(loop, sock, _staged({"kept.txt": "kept\n"}))

    with pytest.raises(gt.GuestLoopError, match="stream dropped"):
        await _drive(monkeypatch, crashed)
    assert (settings.projects_dir / "fx1" / "kept.txt").read_text() == "kept\n"


async def test_a_completed_turn_does_not_pull_again(env, monkeypatch):
    calls = []
    _pull_returns(monkeypatch, {}, calls=calls)

    async def fine(loop, sock, spec):
        await _send(loop, sock, {"type": "final", "content": "ok"})
        await _send(loop, sock, _staged({"e.txt": "e\n"}))

    await _drive(monkeypatch, fine)
    assert calls == []
    assert (settings.projects_dir / "fx1" / "e.txt").read_text() == "e\n"


async def test_a_stop_cancels_what_the_guest_had_brokered(env, monkeypatch):
    """Whatever the host was running for the turn (a brokered child agent, a model
    stream) is cancelled in the same finally, first, so a stop really stops it."""
    seen = []
    monkeypatch.setattr(broker, "cancel_inflight", lambda op: seen.append(op), raising=False)
    _pull_returns(monkeypatch, {})
    await _stop_after_first_token(monkeypatch)
    # first in the finally; release_token (FX2) cancels again on the way out,
    # which is harmless: cancel_inflight is idempotent
    assert seen and set(seen) == {"op-fx1"}


# --- ROBUST-03 ------------------------------------------------------------------

async def test_refused_and_failed_files_are_told_in_the_answer(env, monkeypatch):
    from backend import secrets as secrets_mod
    from backend import writes
    secrets_mod.save({"STRIPE_KEY": "sk_live_abcdef123456"})
    real = writes.apply_write

    async def flaky(slug, rel, content):
        if rel == "b.txt":
            raise OSError(28, "No space left on device")
        return await real(slug, rel, content)
    monkeypatch.setattr(writes, "apply_write", flaky)

    async def scenario(loop, sock, spec):
        await _send(loop, sock, {"type": "token", "content": "writing"})
        await _send(loop, sock, {"type": "final", "content": "All three files are written."})
        await _send(loop, sock, _staged({"config.js": "const k = 'sk_live_abcdef123456';\n",
                                         "b.txt": "b\n", "ok.txt": "fine\n"}))

    events, _ = await _drive(monkeypatch, scenario)
    assert [e["type"] for e in events] == ["token", "final"]
    text = events[-1]["content"]
    assert text.startswith("All three files are written.")
    assert "config.js" in text and "STRIPE_KEY" in text and "{{secret:STRIPE_KEY}}" in text
    assert "b.txt" in text and "No space left on device" in text
    assert "ok.txt" not in text                       # what landed is not listed
    assert "sk_live_abcdef123456" not in text         # names, never values
    proj = settings.projects_dir / "fx1"
    assert (proj / "ok.txt").read_text() == "fine\n"
    assert not (proj / "config.js").exists() and not (proj / "b.txt").exists()
    # ...and the operator has a record of the write that failed for a reason
    # that is not a secret (the refusal already raised its own event)
    summaries = [e["summary"] for e in await _events("write_flag")]
    assert any("b.txt" in x for x in summaries)
    assert any("config.js" in x and "secret" in x for x in summaries)


async def test_a_clean_turn_answer_is_untouched(env, monkeypatch):
    async def scenario(loop, sock, spec):
        await _send(loop, sock, {"type": "final", "content": "done"})
        await _send(loop, sock, _staged({"ok.txt": "fine\n"}))

    events, _ = await _drive(monkeypatch, scenario)
    assert events == [{"type": "final", "content": "done"}]


async def test_a_protected_path_is_refused_in_the_open(env, monkeypatch):
    async def scenario(loop, sock, spec):
        await _send(loop, sock, {"type": "final", "content": "done"})
        await _send(loop, sock, _staged({".context.json": "{}", "ok.txt": "fine\n"}))

    events, _ = await _drive(monkeypatch, scenario)
    assert ".context.json" in events[0]["content"]
    assert not (settings.projects_dir / "fx1" / ".context.json").exists()


async def test_a_corrupt_write_buffer_does_not_fail_the_turn(env, monkeypatch):
    async def scenario(loop, sock, spec):
        await _send(loop, sock, {"type": "final", "content": "done"})
        await _send(loop, sock, {"type": "staged",
                                 "tar_b64": base64.b64encode(b"not a tar").decode()})

    events, _ = await _drive(monkeypatch, scenario)
    assert events[0]["content"].startswith("done")
    assert "could not be brought back" in events[0]["content"]


async def test_a_nested_turn_answer_is_not_held(env, monkeypatch):
    """Only the workspace owner receives `staged`, so only its answer waits."""
    guest_sock, ctl = _wire(monkeypatch)
    loop = asyncio.get_running_loop()

    async def guest():
        await _read_spec(loop, guest_sock)
        await _send(loop, guest_sock, {"type": "final", "content": "child done"})
        guest_sock.close()

    task = asyncio.create_task(guest())

    async def consume():
        return [ev async for ev in gt.guest_turn(
            8, "sys", [], op_id="op-fx1-child", envelope=_env("op-fx1-child"),
            active_slug="fx1", push_workspace=False)]
    events = await asyncio.wait_for(consume(), 10)
    await task
    assert events == [{"type": "final", "content": "child done"}]
    broker.release_token("op-fx1-child")
