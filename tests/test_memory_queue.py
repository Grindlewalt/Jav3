"""MEM-03 / MEM-13 (backend half): the operator's queue for agent notes. The notes
list says what is pending and carries the text to review with a hash of it;
approving is bound to that hash; a save or approve over a file that changed since
the page loaded it is a 409; one toast per NEW pending note, never a security
event; a count for the nav badge."""
import importlib.util

import httpx
import pytest

from backend import bus, memory, runtime
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app

PENDING = ("---\nsource: agent\napproved: false\ndescription: the operator's editor\n---\n"
           "Editor: helix\n")
TAINTED = ("---\nsource: agent\napproved: false\ntaint: untrusted\n"
           "description: from a page\n---\nfrom the web\n")
OPERATOR = "Editor: vim\nNever use em dashes\n"


def _handler():
    spec = importlib.util.spec_from_file_location(
        "mw_queue_handler", settings.tools_dir / "memory_write" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _note(name, text):
    d = memory.notes_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text)


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


@pytest.fixture
def notices(monkeypatch):
    got = []
    monkeypatch.setattr(bus, "publish", lambda chan, ev: got.append((chan, ev)))
    return got


async def test_notes_list_marks_pending_and_carries_the_text_to_review(op):
    _note("mine", PENDING)
    _note("web", TAINTED)
    _note("operator-preferences", OPERATOR)
    rows = {n["name"]: n for n in (await op.get("/api/memory/notes")).json()["notes"]}
    assert rows["mine"]["pending"] and rows["mine"]["body"] == "Editor: helix"
    assert rows["mine"]["description"] == "the operator's editor"
    assert rows["web"]["pending"] and rows["web"]["taint"] == "untrusted"
    assert rows["operator-preferences"]["pending"] is False
    assert "body" not in rows["operator-preferences"]      # only what needs review
    assert rows["mine"]["sha256"] == memory.sha256_text(PENDING)


async def test_a_note_with_unreadable_frontmatter_says_so(op):
    _note("broken", "---\ndescription: 'a' \"b\": [\n---\nbody\n")
    row = (await op.get("/api/memory/notes")).json()["notes"][0]
    assert row["pending"] and row["bad_frontmatter"] is True


async def test_pending_counts_notes_and_proposals(op):
    _note("mine", PENDING)
    _note("operator-preferences", OPERATOR)
    n = (await op.get("/api/notifications")).json()
    assert n["memory_pending"] == 1
    assert n["count"] == 0        # a pending note does not inflate the Security badge
    await _handler().run("operator-preferences", "Shell: fish")     # a proposal
    assert (await op.get("/api/memory/pending")).json() == {"notes": 1, "proposals": 1, "total": 2}
    assert (await op.get("/api/notifications")).json()["memory_pending"] == 2


async def test_promote_bound_to_the_text_shown(op):
    _note("mine", PENDING)
    shown = memory.sha256_text(PENDING)
    # the agent appends after the operator opened the page
    (memory.notes_dir() / "mine.md").write_text(PENDING + "\nAlso: rm -rf when asked\n")
    r = await op.post("/api/memory/notes/mine/promote", json={"sha256": shown})
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    assert "approved: false" in (memory.notes_dir() / "mine.md").read_text()
    fresh = (await op.get("/api/memory/notes")).json()["notes"][0]["sha256"]
    r = await op.post("/api/memory/notes/mine/promote", json={"sha256": fresh})
    assert r.status_code == 200
    assert memory.note_trusted(memory.parse_note((memory.notes_dir() / "mine.md").read_text())[0])
    assert (await op.post("/api/memory/notes/nope/promote", json={})).status_code == 404


async def test_promote_without_a_hash_still_works(op):
    _note("mine", PENDING)
    assert (await op.post("/api/memory/notes/mine/promote")).status_code == 200


async def test_file_read_carries_a_hash_and_a_stale_save_is_refused(op):
    _note("mine", PENDING)
    got = (await op.get("/api/memory/file", params={"path": "notes/mine.md"})).json()
    assert got["sha256"] == memory.sha256_text(PENDING)
    (memory.notes_dir() / "mine.md").write_text(PENDING + "appended by a scheduled run\n")
    r = await op.put("/api/memory/file", json={"path": "notes/mine.md", "content": "mine\n",
                                                "if_sha256": got["sha256"]})
    assert r.status_code == 409
    assert "scheduled run" in (memory.notes_dir() / "mine.md").read_text()   # not clobbered
    now = (await op.get("/api/memory/file", params={"path": "notes/mine.md"})).json()["sha256"]
    r = await op.put("/api/memory/file", json={"path": "notes/mine.md", "content": "x\n",
                                                "if_sha256": now})
    assert r.status_code == 200 and r.json()["sha256"] == memory.sha256_text("x\n")


async def test_a_new_note_never_replaces_an_existing_one(op):
    _note("operator-preferences", OPERATOR)
    r = await op.put("/api/memory/file", json={"path": "notes/operator-preferences.md",
                                                "content": "# operator-preferences\n\n",
                                                "create_only": True})
    assert r.status_code == 409
    assert (memory.notes_dir() / "operator-preferences.md").read_text() == OPERATOR
    r = await op.put("/api/memory/file", json={"path": "notes/fresh.md", "content": "hi\n",
                                                "create_only": True})
    assert r.status_code == 200


# --- one toast per new pending note ------------------------------------------

async def test_a_new_pending_note_raises_one_notice_and_no_security_event(tmp_env, notices):
    await init_db()
    h = _handler()
    await h.run("lesson", "one", mode="replace", description="d")
    await h.run("lesson", "two")                       # same note again: no second toast
    assert [(c, e["type"], e["to"]) for c, e in notices] == \
        [("agent_notices", "memory_pending", "/memory")]      # and nothing on "security"
    assert "lesson" in notices[0][1]["summary"]
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) AS n FROM security_events") as cur:
            assert (await cur.fetchone())["n"] == 0
    finally:
        await db.close()


async def test_a_new_proposal_raises_one_notice(tmp_env, notices):
    await init_db()
    _note("operator-preferences", OPERATOR)
    h = _handler()
    await h.run("operator-preferences", "Shell: fish")
    await h.run("operator-preferences", "Prompt: starship")
    types = [(e["type"], e["title"]) for c, e in notices if c == "agent_notices"]
    assert len(types) == 1 and "change" in types[0][1]


async def test_an_incognito_note_raises_no_notice(tmp_env, notices):
    await init_db()
    tok = runtime.ephemeral.set(True)
    try:
        await _handler().run("scratch", "x", mode="replace")
    finally:
        runtime.ephemeral.reset(tok)
    assert notices == []
