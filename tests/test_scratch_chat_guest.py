"""WEBA-01: a chat with no project loaded used to offer write_file/edit_file and
then fail in the guest ('no project is loaded'), after which the model loaded an
unrelated project and the chat was re-pinned to it. Now the turn's workspace IS
the chat's artifact store (chat-<id>): the host hands that slug to the guest,
the guest's writes land there at turn end, and the store is registered as a
hidden project once it holds a file. Offline: chat.guest_turn is substituted."""
import httpx
import pytest

from backend import chat as chat_mod
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


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
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        yield c


def _turn(seen: dict, write: str | None):
    async def turn(cid, system_prompt, history, tools=None, **kw):
        seen.update(kw, cid=cid)
        if write:
            # what apply_guest_writes does at turn end for the workspace owner
            dest = settings.projects_dir / kw["active_slug"] / write
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("hello")
        yield {"type": "final", "content": "done"}
    return turn


async def test_no_project_turn_gets_the_chat_store_as_its_workspace(client, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(chat_mod, "guest_turn", _turn(seen, "a0-note.md"))
    r = await client.post("/api/chat", json={"message": "make a note",
                                             "project_mode": "none", "confirm_peak": True})
    assert r.status_code == 200
    slug = f"chat-{seen['cid']}"
    assert seen["active_slug"] == slug and seen["push_workspace"] is True
    assert (settings.projects_dir / slug / "a0-note.md").read_text() == "hello"
    # the store is registered (hidden) once it holds a file, so /artifacts lists it
    arts = (await client.get("/api/artifacts")).json()["artifacts"]
    assert [a["slug"] for a in arts] == [slug]
    assert [f["path"] for f in arts[0]["files"]] == ["a0-note.md"]
    assert slug not in {p["slug"] for p in (await client.get("/api/projects")).json()["projects"]}


async def test_a_chat_that_writes_nothing_leaves_no_store_behind(client, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(chat_mod, "guest_turn", _turn(seen, None))
    await client.post("/api/chat", json={"message": "hi", "confirm_peak": True})
    assert seen["active_slug"] == f"chat-{seen['cid']}"
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) n FROM projects") as cur:
            assert (await cur.fetchone())["n"] == 0
    finally:
        await db.close()
    assert not (settings.projects_dir / f"chat-{seen['cid']}").exists()


async def test_a_real_project_is_untouched(client, monkeypatch):
    seen: dict = {}
    r = await client.post("/api/projects", json={"name": "Real one", "summary": "x"})
    assert r.status_code in (200, 201), r.text
    slug = r.json().get("slug") or "real-one"
    monkeypatch.setattr(chat_mod, "guest_turn", _turn(seen, None))
    await client.post("/api/chat", json={"message": "hi", "project": slug, "confirm_peak": True})
    assert seen["active_slug"] == slug


async def test_load_project_refuses_in_a_chat_opened_with_no_project(client, monkeypatch):
    """The 'No project' chip locks the chat to none. The model then loaded
    another agent's live project and the chat was re-pointed to it (WEBA-01)."""
    from backend.agent.tools import registry
    from backend import runtime
    seen: dict = {}
    r = await client.post("/api/projects", json={"name": "Other", "summary": "x"})
    other = r.json().get("slug") or "other"
    monkeypatch.setattr(chat_mod, "guest_turn", _turn(seen, None))
    await client.post("/api/chat", json={"message": "hi", "project_mode": "none",
                                         "confirm_peak": True})
    cid = seen["cid"]
    tok = runtime.conversation_id.set(cid)
    try:
        out = await registry.dispatch("load_project", {"slug": other})
    finally:
        runtime.conversation_id.reset(tok)
    assert out.startswith("error:") and "No project" in out
    assert (await client.get(f"/api/conversations/{cid}/info")).json()["project"] in (None, "")
