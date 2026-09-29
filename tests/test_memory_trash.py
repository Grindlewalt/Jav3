"""MEM-10: deleting a note goes through a recoverable trash and leaves an audit
event; an agent cannot delete a binding (trusted) note from a tainted turn; the
operator has a delete and a restore of their own."""
import importlib.util

import httpx
import pytest

from backend import memory, runtime
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.vm import broker


def _handler():
    spec = importlib.util.spec_from_file_location(
        "mw_trash_handler", settings.tools_dir / "memory_write" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _note(name, text):
    d = memory.notes_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text)


PENDING = "---\nsource: agent\napproved: false\ndescription: my lesson\n---\nprefer small commits\n"
OPERATOR = "Editor: vim\nNever use em dashes\n"


async def _events(kind):
    db = await get_db()
    try:
        async with db.execute("SELECT severity, summary, detail FROM security_events "
                              "WHERE kind = ? ORDER BY id", (kind,)) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def test_agent_delete_moves_the_note_to_the_trash(tmp_env):
    await init_db()
    _note("lesson", PENDING)
    out = await _handler().run("lesson", "", mode="delete")
    assert "deleted" in out and "trash" in out.lower()
    assert not (memory.notes_dir() / "lesson.md").exists()
    items = memory.list_trash()
    assert [i["name"] for i in items] == ["lesson"]
    assert "prefer small commits" in (memory.notes_dir() / ".trash" / f"{items[0]['id']}.md").read_text()


async def test_trashed_note_is_restorable_byte_for_byte(tmp_env):
    await init_db()
    _note("lesson", PENDING)
    await _handler().run("lesson", "", mode="delete")
    tid = memory.list_trash()[0]["id"]
    assert memory.restore_trash(tid) == "lesson"
    assert (memory.notes_dir() / "lesson.md").read_text() == PENDING
    assert memory.list_trash() == []


async def test_restore_refuses_to_overwrite_a_note_that_exists(tmp_env):
    await init_db()
    _note("lesson", PENDING)
    await _handler().run("lesson", "", mode="delete")
    _note("lesson", "a newer note\n")
    with pytest.raises(FileExistsError):
        memory.restore_trash(memory.list_trash()[0]["id"])
    assert (memory.notes_dir() / "lesson.md").read_text() == "a newer note\n"
    assert len(memory.list_trash()) == 1


async def test_two_deletes_of_the_same_name_keep_both(tmp_env):
    await init_db()
    for text in ("first\n", "second\n"):
        _note("n", PENDING.replace("prefer small commits", text.strip()))
        await _handler().run("n", "", mode="delete")
    assert len(memory.list_trash()) == 2


async def test_deleting_a_trusted_note_from_a_clean_turn_is_audited(tmp_env):
    await init_db()
    _note("operator-preferences", OPERATOR)
    out = await _handler().run("operator-preferences", "", mode="delete")
    assert "deleted" in out
    ev = await _events("memory_deleted")
    assert len(ev) == 1 and ev[0]["severity"] == "warn"
    assert "operator-preferences" in ev[0]["summary"]
    assert memory.list_trash()[0]["name"] == "operator-preferences"


async def test_deleting_a_pending_note_is_a_record_only_event(tmp_env):
    await init_db()
    _note("lesson", PENDING)
    await _handler().run("lesson", "", mode="delete")
    ev = await _events("memory_deleted")
    assert len(ev) == 1 and ev[0]["severity"] == "info"


async def test_tainted_turn_cannot_delete_a_trusted_note(tmp_env):
    await init_db()
    _note("operator-preferences", OPERATOR)
    tok = runtime.write_taint.set("untrusted")
    try:
        out = await _handler().run("operator-preferences", "", mode="delete")
    finally:
        runtime.write_taint.reset(tok)
    assert out.startswith("error: refused")
    assert (memory.notes_dir() / "operator-preferences.md").read_text() == OPERATOR
    assert memory.list_trash() == []
    ev = await _events("memory_refused")
    assert len(ev) == 1 and "operator-preferences" in ev[0]["summary"]


async def test_tainted_turn_may_still_delete_its_own_pending_note(tmp_env):
    await init_db()
    _note("lesson", PENDING)
    tok = runtime.write_taint.set("untrusted")
    try:
        out = await _handler().run("lesson", "", mode="delete")
    finally:
        runtime.write_taint.reset(tok)
    assert "deleted" in out
    assert [i["name"] for i in memory.list_trash()] == ["lesson"]


async def test_through_the_broker_after_a_web_read(tmp_env):
    """The broker is what sets the taint for memory_write: web_read, then a
    delete of the operator's note must be refused end to end."""
    await init_db()
    _note("operator-preferences", OPERATOR)
    broker.register_turn(broker.TurnEnvelope(op_id="op-del", web_session="ws"))
    try:
        broker.mark_tainted("op-del")
        out = await broker.broker_dispatch("op-del", "memory_write", {
            "name": "operator-preferences", "content": "", "mode": "delete"})
    finally:
        broker.release_turn("op-del")
    assert "refused" in out["result"]
    assert (memory.notes_dir() / "operator-preferences.md").exists()


async def test_ephemeral_delete_raises_no_event(tmp_env):
    await init_db()
    tok = runtime.ephemeral.set(True)
    try:
        _note("scratch", PENDING)
        await _handler().run("scratch", "", mode="delete")
    finally:
        runtime.ephemeral.reset(tok)
    assert await _events("memory_deleted") == []


# --- the operator's own delete and restore ------------------------------------

@pytest.fixture
async def op(tmp_env):
    await init_db()
    memory.ensure_memory_seeds()
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
        yield c


async def test_operator_can_delete_and_restore(op):
    _note("homelab", "Tailscale only\n")
    r = await op.delete("/api/memory/notes/homelab")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert not (memory.notes_dir() / "homelab.md").exists()
    listing = (await op.get("/api/memory/trash")).json()["items"]
    assert [i["name"] for i in listing] == ["homelab"]
    r = await op.post(f"/api/memory/trash/{listing[0]['id']}/restore")
    assert r.status_code == 200 and r.json()["name"] == "homelab"
    assert (memory.notes_dir() / "homelab.md").read_text() == "Tailscale only\n"
    ev = await _events("memory_deleted")
    assert ev and ev[-1]["severity"] == "info" and "operator" in ev[-1]["detail"]


async def test_operator_delete_unknown_and_restore_conflicts(op):
    assert (await op.delete("/api/memory/notes/nope")).status_code == 404
    assert (await op.post("/api/memory/trash/20260101T000000Z__ghost/restore")).status_code == 404
    _note("a", "one\n")
    await op.delete("/api/memory/notes/a")
    _note("a", "two\n")
    tid = (await op.get("/api/memory/trash")).json()["items"][0]["id"]
    assert (await op.post(f"/api/memory/trash/{tid}/restore")).status_code == 409


@pytest.mark.parametrize("bad", ["..", ".trash", ".hidden"])
async def test_operator_routes_refuse_path_tricks(op, bad):
    # ".." is normalised away by the client into a different URL (405); the
    # dot-names reach the route and are refused there
    assert (await op.delete(f"/api/memory/notes/{bad}")).status_code in (400, 404, 405)
    assert (await op.post(f"/api/memory/trash/{bad}/restore")).status_code in (400, 404, 405)


async def test_hidden_dirs_are_never_notes(op):
    _note("real", "x\n")
    await op.delete("/api/memory/notes/real")
    assert (await op.get("/api/memory/notes")).json()["notes"] == []     # trash is not a note
    files = [f["path"] for f in (await op.get("/api/memory")).json()["files"]]
    assert not any(".trash" in p for p in files)
