"""Approved persistence inside the guest VM (backend/vm/persist.py).

QEMU, QMP and the guest are all stubbed — the laptop can't run the guest. What
is pinned here is the host-side policy: approval is operator-only and explicit,
the spec key appears only for an approved top-level non-incognito turn, one
project holds the disk at a time, the last turn out unplugs, taint re-plugs it
read-only, a guest that won't let go gets torn down, and the disk is capped.
"""
import asyncio
import importlib.util
import json
import pathlib

import httpx
import pytest

from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds
from backend.vm import broker, lifecycle, persist
from backend.vm import guest_turn as gt

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_state():
    persist.forget()
    yield
    persist.forget()


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
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        await c.post("/api/projects", json={"name": "Demo", "summary": "a demo"})
        yield c


async def _events(kind: str) -> list[dict]:
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM security_events WHERE kind = ?",
                              (kind,)) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


# --- approval API -----------------------------------------------------------------

async def test_default_off_and_approve_needs_acknowledge(client):
    r = await client.get("/api/projects/demo/persist")
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is False and body["mount"] == "/persist"
    assert body["disk"]["exists"] is False
    assert body["disk"]["cap_bytes"] == settings.vm_persist_max_mb * 1024 * 1024

    r = await client.put("/api/projects/demo/persist", json={"approved": True})
    assert r.status_code == 400
    assert not await persist.approved("demo")

    r = await client.put("/api/projects/demo/persist",
                         json={"approved": True, "acknowledge": True})
    assert r.status_code == 200 and r.json()["approved"] is True
    assert r.json()["approved_at"]
    assert await persist.approved("demo")
    ev = await _events("persist_approved")
    assert len(ev) == 1 and ev[0]["project_slug"] == "demo"


async def test_revoke_deletes_disk_and_raises_event(client):
    await client.put("/api/projects/demo/persist",
                     json={"approved": True, "acknowledge": True})
    persist.disk_dir().mkdir(parents=True, exist_ok=True)
    persist.disk_path("demo").write_bytes(b"x")
    persist._ready_marker("demo").touch()

    r = await client.put("/api/projects/demo/persist",
                         json={"approved": False, "delete_disk": True})
    assert r.status_code == 200
    assert r.json()["approved"] is False and r.json()["deleted"] is True
    assert not persist.disk_path("demo").exists()
    assert not persist._ready_marker("demo").exists()
    assert not await persist.approved("demo")
    ev = await _events("persist_revoked")
    assert len(ev) == 1 and json.loads(ev[0]["detail"])["deleted"] is True


async def test_revoke_while_attached_keeps_the_disk(client):
    await client.put("/api/projects/demo/persist",
                     json={"approved": True, "acknowledge": True})
    persist.disk_dir().mkdir(parents=True, exist_ok=True)
    persist.disk_path("demo").write_bytes(b"x")
    persist._state.holder = "demo"            # a live turn holds it
    r = await client.put("/api/projects/demo/persist",
                         json={"approved": False, "delete_disk": True})
    assert r.status_code == 200
    assert r.json()["approved"] is False and r.json()["deleted"] is False
    assert "delete_error" in r.json()
    assert persist.disk_path("demo").exists()


async def test_unknown_project_and_no_cookie(client, tmp_env):
    assert (await client.get("/api/projects/nope/persist")).status_code == 404
    assert (await client.put("/api/projects/nope/persist",
                             json={"approved": False})).status_code == 404
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
        r = await anon.put("/api/projects/demo/persist",
                           json={"approved": True, "acknowledge": True},
                           headers={"Authorization": "Bearer whatever"})
        assert r.status_code == 401
    assert not await persist.approved("demo")


async def test_kill_switch_overrides_approval(client, monkeypatch):
    await client.put("/api/projects/demo/persist",
                     json={"approved": True, "acknowledge": True})
    monkeypatch.setattr(settings, "vm_persist_enabled", False)
    assert not await persist.approved("demo")
    assert await persist.attach_for_turn("demo") is None


# --- the disk ----------------------------------------------------------------------

def test_disk_path_refuses_odd_slugs(tmp_env):
    assert persist.disk_path("demo").name == "demo.qcow2"
    for bad in ("../x", "a/b", "", ".hidden", "Demo", None):
        with pytest.raises(persist.PersistError):
            persist.disk_path(bad)


def test_cap_has_a_floor(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_persist_max_mb", 1)
    assert persist.cap_bytes() == 64 * 1024 * 1024
    monkeypatch.setattr(settings, "vm_persist_max_mb", 2048)
    assert persist.cap_bytes() == 2 * 1024 ** 3


async def test_ensure_disk_creates_capped_and_checks_existing(tmp_env, monkeypatch):
    calls = []
    info = {"format": "qcow2", "virtual-size": persist.cap_bytes()}

    async def fake_img(*args):
        calls.append(args)
        if args[0] == "create":
            pathlib.Path(args[3]).write_bytes(b"")
            return ""
        return json.dumps(info)
    monkeypatch.setattr(persist, "_qemu_img", fake_img)

    assert await persist.ensure_disk("demo") is True          # created -> fresh
    assert calls[0] == ("create", "-f", "qcow2", str(persist.disk_path("demo")),
                        str(persist.cap_bytes()))
    assert persist.disk_path("demo").stat().st_mode & 0o777 == 0o600

    assert await persist.ensure_disk("demo") is True          # never mounted yet
    persist._ready_marker("demo").touch()
    assert await persist.ensure_disk("demo") is False         # mounted before

    info["virtual-size"] = persist.cap_bytes() + 1
    with pytest.raises(persist.PersistError, match="larger"):
        await persist.ensure_disk("demo")
    info.update({"virtual-size": 1, "backing-filename": "/etc/shadow"})
    with pytest.raises(persist.PersistError, match="backing"):
        await persist.ensure_disk("demo")
    info.pop("backing-filename")
    info["format"] = "raw"
    with pytest.raises(persist.PersistError, match="qcow2"):
        await persist.ensure_disk("demo")


# --- the hold (QMP + guest stubbed) -------------------------------------------------

class Rig:
    def __init__(self, monkeypatch, *, stuck=False, mount_ok=True):
        self.qmp_cmds: list[dict] = []
        self.guest: list[dict] = []
        self.torn_down = 0
        self.stuck = stuck
        self.mount_ok = mount_ok

        async def fake_qmp(cmds):
            self.qmp_cmds.extend(cmds)
            out = []
            for c in cmds:
                if c["execute"] == "blockdev-del" and self.stuck:
                    out.append({"error": {"class": "GenericError",
                                          "desc": "Node jpersist is in use"}})
                else:
                    out.append({"return": {}})
            return out

        async def fake_guest(spec, timeout):
            self.guest.append(spec)
            if spec["mode"] == "persist_mount":
                return {"mounted": self.mount_ok, "error": None if self.mount_ok else "x"}
            return {"mounted": False}

        async def fake_ensure(slug):
            return True

        async def fake_teardown():
            self.torn_down += 1
            persist.forget()

        monkeypatch.setattr(persist, "qmp", fake_qmp)
        monkeypatch.setattr(persist, "_guest", fake_guest)
        monkeypatch.setattr(persist, "ensure_disk", fake_ensure)
        monkeypatch.setattr(persist, "_UNPLUG_TIMEOUT", 0.0)
        monkeypatch.setattr(lifecycle.vm, "teardown", fake_teardown)

    def execs(self, name):
        return [c for c in self.qmp_cmds if c["execute"] == name]


async def test_refcounted_hold_one_project_at_a_time(tmp_env, monkeypatch):
    rig = Rig(monkeypatch)
    persist.disk_dir().mkdir(parents=True, exist_ok=True)
    fact = await persist.attach_for_turn("demo")
    assert fact == {"path": "/persist", "read_only": False}
    add = rig.execs("blockdev-add")[0]["arguments"]
    assert add["read-only"] is False
    assert add["file"]["filename"] == str(persist.disk_path("demo"))
    dev = rig.execs("device_add")[0]["arguments"]
    assert dev["bus"] == "jpersist_rp" and dev["serial"] == "jpersist"
    assert rig.guest[0] == {"mode": "persist_mount", "persist_ro": False,
                            "persist_fresh": True}
    assert persist._ready_marker("demo").exists()

    assert await persist.attach_for_turn("demo") == fact        # joiner shares it
    assert len(rig.execs("device_add")) == 1
    assert await persist.attach_for_turn("other") is None       # busy: no disk

    gen = persist.generation()
    await persist.release_for_turn("demo", gen)
    assert rig.execs("device_del") == [] and persist.holder() == "demo"
    await persist.release_for_turn("demo", gen)                 # last one out
    assert rig.guest[-1] == {"mode": "persist_unmount"}
    assert len(rig.execs("device_del")) == 1
    assert persist.holder() is None

    assert await persist.attach_for_turn("other") is not None   # free again


async def test_failed_mount_unplugs_and_runs_without(tmp_env, monkeypatch):
    rig = Rig(monkeypatch, mount_ok=False)
    persist.disk_dir().mkdir(parents=True, exist_ok=True)
    assert await persist.attach_for_turn("demo") is None
    assert len(rig.execs("device_del")) == 1
    assert persist.holder() is None
    assert not persist._ready_marker("demo").exists()


async def test_taint_replugs_read_only(tmp_env, monkeypatch):
    rig = Rig(monkeypatch)
    persist.disk_dir().mkdir(parents=True, exist_ok=True)
    await persist.attach_for_turn("demo")
    await persist.on_taint("other")                  # not the holder: no-op
    assert rig.execs("device_del") == []

    await persist.on_taint("demo")
    assert len(rig.execs("device_del")) == 1
    adds = rig.execs("blockdev-add")
    assert [a["arguments"]["read-only"] for a in adds] == [False, True]
    assert adds[1]["arguments"]["file"]["read-only"] is True
    assert rig.guest[-1]["persist_ro"] is True and rig.guest[-1]["persist_fresh"] is False
    assert persist.status()["read_only"] is True

    await persist.on_taint("demo")                   # already read-only
    assert len(rig.execs("blockdev-add")) == 2


async def test_guest_that_wont_release_gets_torn_down(tmp_env, monkeypatch):
    rig = Rig(monkeypatch, stuck=True)
    await init_db()
    persist.disk_dir().mkdir(parents=True, exist_ok=True)
    await persist.attach_for_turn("demo")
    await persist.release_for_turn("demo", persist.generation())
    assert rig.torn_down == 1
    assert persist.holder() is None
    assert len(await _events("persist_unplug_failed")) == 1


async def test_stale_release_after_teardown_is_ignored(tmp_env, monkeypatch):
    """A turn that held /persist across a nuke must not release the NEXT hold
    a later turn took on the same project."""
    rig = Rig(monkeypatch)
    persist.disk_dir().mkdir(parents=True, exist_ok=True)
    await persist.attach_for_turn("demo")
    old = persist.generation()
    persist.forget()                                  # the guest was torn down
    await persist.attach_for_turn("demo")
    await persist.release_for_turn("demo", old)       # the stale turn ends
    assert persist.holder() == "demo" and rig.execs("device_del") == []


async def test_teardown_forgets_the_hold(tmp_env, monkeypatch):
    async def noop():
        return None
    monkeypatch.setattr(lifecycle.vm, "_kill_orphans", noop)
    persist._state.holder, persist._state.count = "demo", 2
    await lifecycle.vm.teardown()
    assert persist.holder() is None and persist.status()["turns"] == 0


# --- guest_turn: the spec key ------------------------------------------------------

def _capture_spec(monkeypatch, *, approved=True):
    """Stub guest_turn's transport; return (sent-spec list, attach/release log)."""
    sent: list[dict] = []
    log: list[tuple] = []
    monkeypatch.setattr(gt.workspace_xfer, "build_merged_tar", lambda s: b"")

    async def _acquire():
        return None
    monkeypatch.setattr(lifecycle.vm, "acquire", _acquire)

    async def _approved(slug):
        return approved
    monkeypatch.setattr(persist, "approved", _approved)

    async def _attach(slug):
        log.append(("attach", slug))
        return {"path": "/persist", "read_only": False}

    async def _release(slug, gen):
        log.append(("release", slug))
    monkeypatch.setattr(persist, "attach_for_turn", _attach)
    monkeypatch.setattr(persist, "release_for_turn", _release)

    class FakeSock:
        def connect(self, *a):
            pass

        def setblocking(self, *a):
            pass

        def close(self):
            pass
    monkeypatch.setattr(gt.socket, "socket", lambda *a, **k: FakeSock())
    loop = asyncio.get_running_loop()

    async def _run_in_executor(_ex, fn, *a):
        return fn(*a)

    async def _sendall(_sock, data):
        sent.append(json.loads(data))

    async def _recv(_sock, _n):
        return b""
    monkeypatch.setattr(loop, "run_in_executor", _run_in_executor)
    monkeypatch.setattr(loop, "sock_sendall", _sendall)
    monkeypatch.setattr(loop, "sock_recv", _recv)
    return sent, log


async def _drain(**kw):
    async for _ in gt.guest_turn(1, "sys", [], **kw):
        pass


async def test_spec_key_only_for_approved_top_level_turn(monkeypatch):
    sent, log = _capture_spec(monkeypatch)
    await _drain(active_slug="demo", push_workspace=True, persist=True)
    assert sent[-1]["persist"] == {"path": "/persist", "read_only": False}
    assert log == [("attach", "demo"), ("release", "demo")]


@pytest.mark.parametrize("kw", [
    {"persist": False, "push_workspace": True},            # caller didn't ask
    {"persist": True, "push_workspace": False},            # nested turn
    {"persist": True, "push_workspace": True,              # incognito
     "envelope": broker.TurnEnvelope(op_id="guest:1", ephemeral=True)},
])
async def test_no_spec_key_otherwise(monkeypatch, kw):
    sent, log = _capture_spec(monkeypatch)
    await _drain(active_slug="demo", **kw)
    assert "persist" not in sent[-1] and log == []


async def test_no_spec_key_when_not_approved(monkeypatch):
    sent, log = _capture_spec(monkeypatch, approved=False)
    await _drain(active_slug="demo", push_workspace=True, persist=True)
    assert "persist" not in sent[-1] and log == []


async def test_callers_never_ask_for_incognito():
    """chat.py and run_agent_turn gate the flag on ephemeral themselves too."""
    chat = (ROOT / "backend/chat.py").read_text()
    assert "persist=not ephemeral" in chat
    turn = (ROOT / "backend/vm/turn.py").read_text()
    assert "persist=(not nested and not runtime.ephemeral.get())" in turn


# --- broker: taint flips it read-only before the result goes back ------------------

async def test_first_taint_triggers_read_only(monkeypatch):
    seen = []

    async def _on_taint(slug):
        seen.append(slug)
    monkeypatch.setattr(persist, "on_taint", _on_taint)

    async def _dispatch(name, args):
        return "page text"
    monkeypatch.setattr(broker.registry, "dispatch", _dispatch)
    env = broker.TurnEnvelope(op_id="guest:77", active_project="demo")
    broker.register_turn(env)
    try:
        await broker.broker_dispatch("guest:77", "list_files", {})
        assert seen == []
        await broker.broker_dispatch("guest:77", "web_read", {})
        assert seen == ["demo"]
        await broker.broker_dispatch("guest:77", "web_read", {})
        assert seen == ["demo"]                       # only the first taint
    finally:
        broker.release_turn("guest:77")


# --- guest side ----------------------------------------------------------------------

def _guest_persist():
    spec = importlib.util.spec_from_file_location(
        "guest_persist_under_test", ROOT / "guest/backend/persist.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_guest_note_only_when_mounted():
    g = _guest_persist()
    assert g.note(None) == "" and g.note({}) == ""
    rw = g.note({"path": "/persist", "read_only": False})
    assert "/persist" in rw and "survive" in rw
    assert "READ-ONLY" in g.note({"path": "/persist", "read_only": True})
    assert g.SERIAL == persist.SERIAL


def test_guest_mount_options(monkeypatch, tmp_path):
    g = _guest_persist()
    ran = []
    dev = tmp_path / "vdb"
    dev.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(g, "_find_device", lambda: dev)
    monkeypatch.setattr(g, "_mounted", lambda: False)
    monkeypatch.setattr(g, "MOUNT", tmp_path / "persist")

    class R:
        returncode = 0
    monkeypatch.setattr(g.subprocess, "run", lambda cmd, **k: ran.append(cmd) or R())

    r = g.mount(read_only=False, fresh=True)
    assert r["mounted"] and r["formatted"]
    assert ran[0][0] == "mkfs.ext4"
    assert "noexec,nodev,nosuid" in ran[1][ran[1].index("-o") + 1]

    ran.clear()
    r = g.mount(read_only=True, fresh=False)
    assert not r["formatted"] and ran[0][0] == "mount"
    assert ran[0][ran[0].index("-o") + 1].endswith(",ro,noload")

    ran.clear()
    dev.write_bytes(b"\1" + b"\0" * 4095)              # not blank: never mkfs
    g.mount(read_only=False, fresh=True)
    assert ran[0][0] == "mount"
