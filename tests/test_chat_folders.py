"""Chat folders and stars — sidebar organisation over the conversations table.

A folder is a name and a place in the order; a chat sits in at most one
(conversations.folder_id). Deleting a folder unfiles its chats, never deletes
them. Starring is a flag on the row. Neither may disturb the project binding
the same PATCH endpoint also sets.
"""
import asyncio

import httpx
import pytest

from backend.auth import hash_password
from backend.db import get_db, init_db, open_conversation
from backend.main import app
from backend.memory import ensure_memory_seeds


@pytest.fixture
async def client(tmp_env):
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
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


async def _chat(title: str, kind: str = "chat") -> int:
    db = await get_db()
    try:
        return await open_conversation(db, project=None, title=title, kind=kind)
    finally:
        await db.close()


async def _convos(client, **params) -> list[dict]:
    r = await client.get("/api/conversations", params=params)
    assert r.status_code == 200, r.text
    return r.json()["conversations"]


async def _folder(client, name: str) -> dict:
    r = await client.post("/api/chat/folders", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["folder"]


async def _settle():
    from backend import chat as chat_mod
    for _ in range(600):
        if not chat_mod._active_turns:
            return
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_folder_crud(client):
    assert (await client.get("/api/chat/folders")).json() == {"folders": []}
    f = await _folder(client, "  Work   stuff ")
    assert f["name"] == "Work stuff", "whitespace should collapse"
    assert f["count"] == 0

    r = await client.patch(f"/api/chat/folders/{f['id']}", json={"name": "Work"})
    assert r.status_code == 200 and r.json()["folder"]["name"] == "Work"

    assert (await client.post("/api/chat/folders", json={"name": "  "})).status_code == 400
    assert (await client.post("/api/chat/folders", json={"name": "work"})).status_code == 409
    assert (await client.patch("/api/chat/folders/9999", json={"name": "x"})).status_code == 404

    assert (await client.delete(f"/api/chat/folders/{f['id']}")).status_code == 200
    assert (await client.get("/api/chat/folders")).json()["folders"] == []
    assert (await client.delete(f"/api/chat/folders/{f['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_folders_keep_order_and_move(client):
    a, b, c = [await _folder(client, n) for n in ("A", "B", "C")]
    names = lambda fs: [f["name"] for f in fs]          # noqa: E731
    assert names((await client.get("/api/chat/folders")).json()["folders"]) == ["A", "B", "C"]

    r = await client.patch(f"/api/chat/folders/{c['id']}", json={"position": 0})
    assert names(r.json()["folders"]) == ["C", "A", "B"]
    # positions are renumbered densely, so a later move lands where asked
    assert [f["position"] for f in r.json()["folders"]] == [0, 1, 2]
    r = await client.patch(f"/api/chat/folders/{c['id']}", json={"position": 99})
    assert names(r.json()["folders"]) == ["A", "B", "C"]
    # a new folder goes to the end
    await _folder(client, "D")
    assert names((await client.get("/api/chat/folders")).json()["folders"])[-1] == "D"


@pytest.mark.asyncio
async def test_star_toggle_leaves_project_binding_alone(client):
    await client.post("/api/projects", json={"name": "Pinned"})
    cid = await _chat("starred one")
    r = await client.patch(f"/api/conversations/{cid}",
                           json={"project": "pinned", "mode": "pin"})
    assert r.status_code == 200, r.text

    r = await client.patch(f"/api/conversations/{cid}", json={"starred": True})
    assert r.status_code == 200 and r.json()["starred"] is True
    row = next(c for c in await _convos(client) if c["id"] == cid)
    assert row["starred"] is True
    assert row["project_slug"] == "pinned", "a star toggle unpinned the chat"
    assert row["project_locked"] == 1

    await client.patch(f"/api/conversations/{cid}", json={"starred": False})
    row = next(c for c in await _convos(client) if c["id"] == cid)
    assert row["starred"] is False and row["project_slug"] == "pinned"


@pytest.mark.asyncio
async def test_move_to_folder_and_back(client):
    f = await _folder(client, "Research")
    cid = await _chat("filed")
    r = await client.patch(f"/api/conversations/{cid}", json={"folder_id": f["id"]})
    assert r.status_code == 200 and r.json()["folder_id"] == f["id"]
    assert (await client.get("/api/chat/folders")).json()["folders"][0]["count"] == 1

    # a rename alone doesn't unfile it
    await client.patch(f"/api/conversations/{cid}", json={"title": "renamed"})
    row = next(c for c in await _convos(client) if c["id"] == cid)
    assert row["folder_id"] == f["id"] and row["summary"] == "renamed"

    # explicit null unfiles
    await client.patch(f"/api/conversations/{cid}", json={"folder_id": None})
    assert next(c for c in await _convos(client) if c["id"] == cid)["folder_id"] is None

    r = await client.patch(f"/api/conversations/{cid}", json={"folder_id": 9999})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_deleting_a_folder_unfiles_its_chats(client):
    f = await _folder(client, "Doomed")
    ids = [await _chat(f"c{i}") for i in range(2)]
    for cid in ids:
        await client.patch(f"/api/conversations/{cid}", json={"folder_id": f["id"]})
    r = await client.delete(f"/api/chat/folders/{f['id']}")
    assert r.json() == {"ok": True, "unfiled": 2}
    rows = {c["id"]: c for c in await _convos(client)}
    assert all(rows[cid]["folder_id"] is None for cid in ids), "chats must survive, unfiled"


@pytest.mark.asyncio
async def test_list_filter(client):
    f = await _folder(client, "F")
    filed, starred, loose = await _chat("filed"), await _chat("star"), await _chat("loose")
    await _chat("job node", kind="subagent")
    await client.patch(f"/api/conversations/{filed}", json={"folder_id": f["id"]})
    await client.patch(f"/api/conversations/{starred}", json={"starred": True})

    ids = lambda rows: {c["id"] for c in rows}           # noqa: E731
    assert ids(await _convos(client, folder=str(f["id"]))) == {filed}
    assert ids(await _convos(client, folder="starred")) == {starred}
    assert ids(await _convos(client, folder="none")) == {starred, loose}
    assert ids(await _convos(client)) == {filed, starred, loose}, "job nodes stay out"
    assert (await client.get("/api/conversations",
                             params={"folder": "bogus"})).status_code == 400
    # newest first, id breaking same-second ties
    assert [c["id"] for c in await _convos(client)] == [loose, starred, filed]


@pytest.mark.asyncio
async def test_deleting_a_filed_starred_chat(client):
    f = await _folder(client, "Keep")
    cid = await _chat("gone")
    await client.patch(f"/api/conversations/{cid}",
                       json={"folder_id": f["id"], "starred": True})
    assert (await client.delete(f"/api/conversations/{cid}")).status_code == 200
    assert (await client.get("/api/chat/folders")).json()["folders"][0]["count"] == 0


@pytest.mark.asyncio
async def test_incognito_wipe_unaffected_by_folder_and_star(client, monkeypatch):
    """An incognito row filed and starred mid-turn is still wiped at turn end,
    and the folder it sat in survives."""
    from backend import chat as chat_mod
    f = await _folder(client, "Private")
    seen: dict = {}

    async def turn(cid, system_prompt, history, **kw):
        db = await get_db()
        try:
            await db.execute(
                "UPDATE conversations SET folder_id = ?, starred = 1 WHERE id = ?",
                (f["id"], cid))
            await db.commit()
        finally:
            await db.close()
        seen["cid"] = cid
        yield {"type": "final", "content": "ok"}

    monkeypatch.setattr(chat_mod, "guest_turn", turn)
    r = await client.post("/api/chat", json={"message": "hi", "ephemeral": True,
                                             "confirm_peak": True})
    assert r.status_code == 200
    await _settle()
    db = await get_db()
    try:
        async with db.execute("SELECT 1 FROM conversations WHERE id = ?",
                              (seen["cid"],)) as cur:
            assert await cur.fetchone() is None, "incognito row survived"
    finally:
        await db.close()
    folders = (await client.get("/api/chat/folders")).json()["folders"]
    assert folders[0]["id"] == f["id"] and folders[0]["count"] == 0


@pytest.mark.asyncio
async def test_folders_require_login(tmp_env):
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/chat/folders")).status_code == 401
        assert (await c.post("/api/chat/folders", json={"name": "x"})).status_code == 401
