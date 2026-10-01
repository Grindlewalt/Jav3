"""What the operator can do about an event (SB2, 2026-10-01): revert a flagged file,
un-cut a host, kill a process the last ps snapshot named, stop the agent or the
whole run. Each with the refusals: a changed file, a reused pid, a run nobody
confirmed."""
import hashlib
import os
import subprocess
import time
from types import SimpleNamespace

import httpx
import pytest

from backend import db as db_mod
from backend import egress, secactions, security
from backend.auth import hash_password
from backend.config import settings
from backend.main import app
from backend.vm import boxes, procview
from test_procview import _materialise, pw


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    egress._cut.clear()
    egress._uncut_until.clear()
    security._pings.clear()
    (settings.projects_dir / "proj").mkdir(parents=True)
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


@pytest.fixture
async def client(db):
    await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                     ("grindlewalt", hash_password("hunter2")))
    await db.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "grindlewalt", "password": "hunter2"})
        yield c


def _git(*args):
    subprocess.run(["git", "-C", str(settings.projects_dir / "proj"),
                    "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def _commit(files: dict):
    root = settings.projects_dir / "proj"
    if not (root / ".git").exists():
        _git("init", "-q")
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _git("add", "-A")
    _git("commit", "-q", "-m", "base")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


async def _flag(db, rel, **detail):
    return await security.raise_event(db, kind="write_flag", project="proj",
                                      summary=f"write flag: x in {rel}",
                                      detail={"path": rel, "trigger": "assertion_removed", **detail})


async def _event(db, eid):
    return await security.get_event(db, eid)


# --- revert a file ---------------------------------------------------------------------

async def test_revert_puts_a_modified_file_back_to_head(db, client):
    _commit({"a.py": "good = 1\n"})
    root = settings.projects_dir / "proj"
    (root / "a.py").write_text("bad = 2\n")
    eid = await _flag(db, "a.py", sha=_sha(b"bad = 2\n"))
    r = await client.post(f"/api/security/events/{eid}/revert")
    assert r.status_code == 200 and r.json()["action"] == "restored"
    assert (root / "a.py").read_text() == "good = 1\n"
    assert (await _event(db, eid))["acknowledged"] == 1
    async with db.execute("SELECT kind, actor, acknowledged, quiet FROM security_events "
                          "WHERE kind = 'write_reverted'") as cur:
        got = [tuple(x) for x in await cur.fetchall()]
    assert got == [("write_reverted", "operator", 1, "operator")]


async def test_revert_refuses_a_file_that_changed_since_the_event(db, client):
    _commit({"a.py": "good = 1\n"})
    root = settings.projects_dir / "proj"
    (root / "a.py").write_text("bad = 2\n")
    eid = await _flag(db, "a.py", sha=_sha(b"bad = 2\n"))
    (root / "a.py").write_text("bad = 2\nmore = 3\n")          # the agent wrote again
    r = await client.post(f"/api/security/events/{eid}/revert")
    assert r.status_code == 409 and "changed since this alert" in r.json()["detail"]
    assert (root / "a.py").read_text() == "bad = 2\nmore = 3\n"      # untouched
    assert (await _event(db, eid))["acknowledged"] == 0


async def test_revert_without_a_fingerprint_goes_by_the_files_mtime(db, client):
    _commit({"a.py": "good = 1\n"})
    root = settings.projects_dir / "proj"
    (root / "a.py").write_text("bad = 2\n")
    eid = await _flag(db, "a.py")                      # an alert from before the sha
    future = time.time() + 600
    os.utime(root / "a.py", (future, future))
    assert (await client.post(f"/api/security/events/{eid}/revert")).status_code == 409
    old = time.time() - 600
    os.utime(root / "a.py", (old, old))
    assert (await client.post(f"/api/security/events/{eid}/revert")).status_code == 200


async def test_revert_deletes_a_file_the_agent_created(db, client):
    _commit({"keep.py": "x\n"})
    root = settings.projects_dir / "proj"
    (root / "sub").mkdir()
    (root / "sub" / "new.py").write_text("hello\n")
    eid = await _flag(db, "sub/new.py", sha=_sha(b"hello\n"), new_file=True)
    r = await client.post(f"/api/security/events/{eid}/revert")
    assert r.json()["action"] == "deleted"
    assert not (root / "sub").exists()                 # the emptied directory goes too
    assert (root / "keep.py").exists()


async def test_revert_refuses_an_uncommitted_file_the_agent_did_not_create(db, client):
    _commit({"keep.py": "x\n"})
    root = settings.projects_dir / "proj"
    (root / "mine.py").write_text("operator's own\n")
    eid = await _flag(db, "mine.py", sha=_sha(b"operator's own\n"), new_file=False)
    r = await client.post(f"/api/security/events/{eid}/revert")
    assert r.status_code == 409 and "never committed" in r.json()["detail"]
    assert (root / "mine.py").exists()


async def test_revert_brings_back_a_deleted_file_only_if_it_is_still_gone(db, client):
    _commit({"gone.py": "was here\n"})
    root = settings.projects_dir / "proj"
    (root / "gone.py").unlink()
    eid = await _flag(db, "gone.py", deleted=True)
    assert (await client.post(f"/api/security/events/{eid}/revert")).json()["action"] == "restored"
    assert (root / "gone.py").read_text() == "was here\n"
    again = await _flag(db, "gone.py", deleted=True)
    r = await client.post(f"/api/security/events/{again}/revert")        # it is back now
    assert r.status_code == 409 and "is back" in r.json()["detail"]
    (root / "never.py").write_text("x")
    never = await _flag(db, "never.py", deleted=True)
    (root / "never.py").unlink()
    assert (await client.post(f"/api/security/events/{never}/revert")).status_code == 409


@pytest.mark.parametrize("kw,why", [
    ({"refused": True}, "refused"),
    ({"sha": "x"}, "no git history"),
])
async def test_revert_other_refusals(db, client, kw, why):
    (settings.projects_dir / "proj" / "a.py").write_text("x")
    eid = await _flag(db, "a.py", **kw)
    r = await client.post(f"/api/security/events/{eid}/revert")
    assert r.status_code == 409 and why in r.json()["detail"]


async def test_revert_refuses_protected_and_escaping_paths_and_other_kinds(db, client):
    _commit({"a.py": "x\n"})
    for rel in (".git/config", "../outside.py", "/etc/passwd"):
        eid = await _flag(db, rel, sha="x")
        assert (await client.post(f"/api/security/events/{eid}/revert")).status_code == 409
    other = await security.raise_event(db, kind="login_failed", summary="x")
    assert (await client.post(f"/api/security/events/{other}/revert")).status_code == 409
    assert (await client.post("/api/security/events/999/revert")).status_code == 404


async def test_revert_of_a_write_the_harness_made_matches_its_own_sha(db, client):
    """End to end: the sha write_flag carries is the one revert checks."""
    from backend import writes
    _commit({"tests/w.test.mjs": "test('a', () => {\n  expect(1)\n  expect(2)\n})\n"})
    await writes.apply_write("proj", "tests/w.test.mjs", b"test('a', () => {\n  expect(1)\n})\n")
    async with db.execute("SELECT id FROM security_events WHERE kind = 'write_flag' "
                          "AND summary LIKE '%assertion_removed%'") as cur:
        eid = (await cur.fetchone())["id"]
    r = await client.post(f"/api/security/events/{eid}/revert")
    assert r.status_code == 200
    assert "expect(2)" in (settings.projects_dir / "proj" / "tests/w.test.mjs").read_text()


# --- un-cut a host ---------------------------------------------------------------------

async def test_uncut_lifts_the_cut_the_drop_and_holds_the_detectors_off(db, client, monkeypatch):
    from backend.vm import egress_proxy
    seen = []

    async def undrop(host, ips=None):
        seen.append((host, list(ips or [])))
        return list(ips or [])
    monkeypatch.setattr(egress_proxy, "nft_undrop", undrop)
    egress.mark_cut("proj", "evil.example")
    eid = await security.raise_event(db, kind="egress_anomaly", severity="critical",
                                     project="proj", summary="high-entropy host evil.example",
                                     detail={"host": "evil.example", "dropped_ips": ["203.0.113.5"]})
    assert egress.is_cut("proj", "evil.example")
    r = await client.post(f"/api/security/events/{eid}/uncut")
    assert r.status_code == 200 and r.json()["was_cut"] is True
    assert not egress.is_cut("proj", "evil.example")
    assert seen == [("evil.example", ["203.0.113.5"])]
    assert egress.in_uncut_grace("proj", "evil.example")
    assert (await _event(db, eid))["acknowledged"] == 1
    verdict, _ = await egress.decide(db, "proj", "evil.example")
    assert verdict != "cut"


async def test_an_uncut_host_is_not_cut_again_straight_away(db, monkeypatch):
    from backend.vm import egress_proxy

    async def drop(host):
        return []
    monkeypatch.setattr(egress_proxy, "_nft_drop", drop)
    att = {"project": "proj", "kind": "project", "box_id": "p-proj", "op_id": None,
           "conversation_id": None, "peer_ip": "10.0.0.2", "peer_port": 40000}
    host = "a8f3k2q9zp1w7v4m.net"
    egress.uncut("proj", host)
    await egress_proxy._record(host, "CONNECT", None, 0, 0, "allow", "allow-by-default", att)
    assert not egress.is_cut("proj", host)
    egress._uncut_until.clear()                                   # grace over
    await egress_proxy._record(host, "CONNECT", None, 0, 0, "allow", "allow-by-default", att)
    assert egress.is_cut("proj", host)


async def test_uncut_refusals(db, client):
    other = await security.raise_event(db, kind="write_flag", summary="x", detail={"host": "h"})
    assert (await client.post(f"/api/security/events/{other}/uncut")).status_code == 409
    nohost = await security.raise_event(db, kind="egress_anomaly", severity="critical",
                                        summary="x", detail={})
    r = await client.post(f"/api/security/events/{nohost}/uncut")
    assert r.status_code == 409 and "host" in r.json()["detail"]


def test_the_grace_ends():
    egress._uncut_until.clear()
    egress.uncut("p", "h.example")
    assert egress.in_uncut_grace("p", "h.example")
    egress._uncut_until[("p", "h.example")] = time.monotonic() - 1
    assert not egress.in_uncut_grace("p", "h.example")


# --- the guest's kill verb ---------------------------------------------------------------

@pytest.fixture
def guest_root(tmp_path):
    return _materialise(tmp_path / "guest")


def _start_ticks(root, pid):
    return pw.parse_stat((root / "proc" / str(pid) / "stat").read_text())["start_ticks"]


def test_the_guest_kills_a_pid_that_still_is_the_process_the_host_saw(guest_root):
    sent = []
    st = _start_ticks(guest_root, 812)
    out = pw.kill_pid(812, "/usr/bin/python3.13", st, root=str(guest_root), self_pid=400,
                      _kill=lambda p, s: sent.append((p, s)))
    assert out == {"ok": True, "pid": 812, "sig": "TERM"} and sent == [(812, 15)]
    sent.clear()
    assert pw.kill_pid(812, "/usr/bin/python3.13", st, sig="KILL", root=str(guest_root),
                       self_pid=400, _kill=lambda p, s: sent.append((p, s)))["ok"]
    assert sent == [(812, 9)]


@pytest.mark.parametrize("pid,exe,ticks,cmd,sig,why", [
    (812, "/usr/bin/python3.13", "wrong", None, "TERM", "changed"),   # started again
    (812, "/usr/bin/curl", "ok", None, "TERM", "changed"),            # the pid was reused
    (812, "/usr/bin/python3.13", None, "other cmd", "TERM", "changed"),
    (777, "/usr/bin/curl", 1, None, "TERM", "gone"),
    (400, "/usr/bin/python3.13", 1, None, "TERM", "protected"),       # the agent server
    (2, "", 1, None, "TERM", "bad_args"),
    (2, "/x", None, None, "TERM", "bad_args"),
    (1, "/usr/lib/systemd/systemd", "ok", None, "TERM", "bad_pid"),
    (812, "/usr/bin/python3.13", "ok", None, "HUP", "bad_signal"),
    (True, "/x", 1, None, "TERM", "bad_pid"),
])
def test_the_guest_refuses_when_it_is_not_that_process(guest_root, pid, exe, ticks, cmd, sig, why):
    if ticks == "ok":
        ticks = _start_ticks(guest_root, pid) if os.path.isdir(guest_root / "proc" / str(pid)) else 1
    sent = []
    out = pw.kill_pid(pid, exe, ticks, cmd, sig, root=str(guest_root), self_pid=400,
                      _kill=lambda p, s: sent.append((p, s)))
    assert out["ok"] is False and out["why"] == why and out["error"]
    assert sent == []


def test_the_guest_refuses_a_kernel_thread(guest_root):
    out = pw.kill_pid(2, "x", 1, root=str(guest_root), self_pid=400, _kill=lambda p, s: 1 / 0)
    assert out["ok"] is False and out["why"] in ("protected", "changed")


def test_the_guest_reports_a_process_that_died_between_check_and_signal(guest_root):
    def boom(p, s):
        raise ProcessLookupError
    out = pw.kill_pid(812, "/usr/bin/python3.13", _start_ticks(guest_root, 812),
                      root=str(guest_root), self_pid=400, _kill=boom)
    assert out["why"] == "gone"


def test_the_servers_speak_the_verb():
    assert 'mode == "kill_pid"' in open("guest/backend/server.py").read()
    assert 'req.get("mode") == "kill_pid"' in open("guest/svc/svcd.py").read()


# --- the host's side of Kill process -------------------------------------------------------

@pytest.fixture
def kbox(tmp_env, monkeypatch, guest_root):
    """A running project box whose last snapshot is the fixture's, and a killer that records."""
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    boxes.registry.reset()
    procview.reset()
    box = SimpleNamespace(id="p-alpha", kind="project", project="alpha", joined=False,
                          is_shared=False)
    monkeypatch.setattr(procview, "_find_box", lambda bid: box if bid == box.id else None)
    monkeypatch.setattr(procview, "_pollable", lambda b: True)
    raw = pw.snapshot(str(guest_root), self_pid=400, ss_text="")
    st = procview.BoxState(box.id)
    st.snap, st.reported_at = procview.sanitize_snapshot(raw), time.time()
    procview._state[box.id] = st
    calls = []

    async def killer(b, spec):
        calls.append(spec)
        return {"type": "kill", "ok": True}
    procview.register_killer("project", killer)
    yield SimpleNamespace(box=box, st=st, calls=calls, snap=st.snap)
    procview._killers.clear()
    procview.reset()
    boxes.registry.reset()


async def _kill(kb, pid=812, exe=None, **kw):
    p = kb.snap["procs"][pid]
    args = dict(cmd=p["cmd"], start_ticks=p["start_ticks"], boot_id=kb.snap["boot_id"])
    args.update(kw)
    return await procview.kill_process("p-alpha", pid, exe or p["exe"], **args)


async def test_the_host_kills_a_pid_the_snapshot_still_holds(kbox):
    out = await _kill(kbox)
    assert out == {"pid": 812, "sig": "TERM", "box_id": "p-alpha"}
    assert kbox.calls[0]["pid"] == 812 and kbox.calls[0]["start_ticks"] == \
        kbox.snap["procs"][812]["start_ticks"]


@pytest.mark.parametrize("change,msg", [
    ({"start_ticks": 5}, "started again"),
    ({"exe": "/usr/bin/curl"}, "now /usr/bin/python3.13"),
    ({"boot_id": "other"}, "rebooted"),
])
async def test_the_host_refuses_a_pid_that_is_not_what_the_alert_named(kbox, change, msg):
    with pytest.raises(procview.KillRefused, match=msg):
        await _kill(kbox, **change)
    assert kbox.calls == []


async def test_the_host_refuses_a_pid_that_is_gone_or_a_stale_snapshot(kbox):
    with pytest.raises(procview.KillRefused, match="no longer running"):
        await procview.kill_process("p-alpha", 4242, "/x", cmd="x")
    kbox.st.reported_at = time.time() - 3600
    with pytest.raises(procview.KillRefused, match="out of date"):
        await _kill(kbox)
    kbox.st.snap = None
    with pytest.raises(procview.KillRefused, match="no process snapshot"):
        await _kill(kbox)
    with pytest.raises(procview.KillRefused, match="not running"):
        await procview.kill_process("p-zzz", 812, "/x", cmd="x")
    assert kbox.calls == []


async def test_the_host_matches_by_command_line_when_there_is_no_start_time(kbox):
    p = kbox.snap["procs"][812]
    assert (await procview.kill_process("p-alpha", 812, p["exe"], cmd=p["cmd"]))["pid"] == 812
    with pytest.raises(procview.KillRefused, match="different command"):
        await procview.kill_process("p-alpha", 812, p["exe"], cmd="python3 -m other")
    with pytest.raises(procview.KillRefused, match="neither a start time"):
        await procview.kill_process("p-alpha", 812, p["exe"])


async def test_the_guests_refusal_and_an_unreachable_box_are_relayed(kbox):
    async def refuse(b, spec):
        return {"type": "kill", "ok": False, "why": "changed", "error": "pid 812 is now curl"}
    procview.register_killer("project", refuse)
    with pytest.raises(procview.KillRefused, match="pid 812 is now curl"):
        await _kill(kbox)

    async def down(b, spec):
        raise ConnectionRefusedError("no route")
    procview.register_killer("project", down)
    with pytest.raises(procview.KillRefused, match="could not reach the box"):
        await _kill(kbox)


async def test_kill_over_http_acks_the_alert_and_files_the_audit_line(db, client, kbox):
    p = dict(kbox.snap["procs"][812])
    eid = await security.raise_event(
        db, kind="unexpected_process", project="alpha", summary="Unexpected process in box p-alpha",
        detail={"box_id": "p-alpha", "pid": 812, "exe": p["exe"], "cmd": p["cmd"],
                "start_ticks": p["start_ticks"], "boot_id": kbox.snap["boot_id"]})
    r = await client.post(f"/api/security/events/{eid}/kill", json={})
    assert r.status_code == 200 and r.json()["pid"] == 812
    assert (await _event(db, eid))["acknowledged"] == 1
    async with db.execute("SELECT kind FROM security_events WHERE kind = 'process_killed'") as cur:
        assert len(await cur.fetchall()) == 1
    # the same alert again: the box now says the pid is some other program
    kbox.snap["procs"][812]["exe"] = "/usr/bin/other"
    again = await security.raise_event(
        db, kind="unexpected_process", project="alpha", summary="Unexpected process again",
        detail={"box_id": "p-alpha", "pid": 812, "exe": p["exe"], "cmd": p["cmd"],
                "start_ticks": p["start_ticks"]})
    r = await client.post(f"/api/security/events/{again}/kill")
    assert r.status_code == 409 and "not killing it" in r.json()["detail"]
    assert (await _event(db, again))["acknowledged"] == 0
    bad = await security.raise_event(db, kind="write_flag", summary="w", detail={"pid": 1})
    assert (await client.post(f"/api/security/events/{bad}/kill")).status_code == 409


# --- stop the agent / the whole run --------------------------------------------------------

async def _conv(db, cid, parent=None, kind="chat", job=None):
    await db.execute("INSERT INTO conversations(id, parent_conversation_id, kind, job_id) "
                     "VALUES (?,?,?,?)", (cid, parent, kind, job))
    await db.commit()


@pytest.fixture
def live(monkeypatch):
    """Pretend these conversations have loops running; record what gets stopped."""
    from backend import chat
    state = SimpleNamespace(ids=set(), stopped=[])
    monkeypatch.setattr(chat, "_running_loops", lambda: set(state.ids))
    monkeypatch.setattr(chat, "_stop", lambda cid: state.stopped.append(cid) or True)
    return state


async def _agent_event(db, cid):
    return await security.raise_event(db, kind="write_flag", project="proj", summary=f"w{cid}",
                                      conversation_id=cid, detail={"path": f"{cid}.py"})


async def test_stop_agent_stops_only_that_conversation(db, client, live):
    await _conv(db, 1)
    await _conv(db, 2, parent=1, kind="agent", job="j")
    await _conv(db, 3, parent=1, kind="agent", job="j")
    live.ids = {1, 2, 3}
    eid = await _agent_event(db, 2)
    r = await client.post(f"/api/security/events/{eid}/stop", json={"scope": "agent"})
    assert r.status_code == 200
    assert r.json()["stopped"] is True and r.json()["agents"] == [2]
    assert live.stopped == [2]


async def test_stop_agent_says_so_when_it_is_not_running(db, client, live):
    await _conv(db, 4)
    eid = await _agent_event(db, 4)
    r = await client.post(f"/api/security/events/{eid}/stop", json={"scope": "agent"})
    assert r.json()["stopped"] is False and "not running" in r.json()["message"]
    assert live.stopped == []


async def test_stop_whole_run_asks_first_and_then_stops_the_live_tree(db, client, live):
    await _conv(db, 10)
    await _conv(db, 11, parent=10, kind="agent", job="j")
    await _conv(db, 12, parent=11, kind="agent", job="j")
    await _conv(db, 20)                                           # another run
    live.ids = {10, 12, 20}
    eid = await _agent_event(db, 12)
    first = (await client.post(f"/api/security/events/{eid}/stop", json={"scope": "run"})).json()
    assert first["needs_confirm"] is True and first["stopped"] is False
    assert first["agents"] == [10, 12] and "2 agents" in first["message"]
    assert live.stopped == []
    dry = (await client.post(f"/api/security/events/{eid}/stop",
                             json={"scope": "run", "dry_run": True, "confirm": True})).json()
    assert dry["stopped"] is False and live.stopped == []
    done = (await client.post(f"/api/security/events/{eid}/stop",
                              json={"scope": "run", "confirm": True})).json()
    assert done["stopped"] is True and sorted(live.stopped) == [10, 12]    # never 20
    async with db.execute("SELECT actor, acknowledged FROM security_events "
                          "WHERE kind = 'run_stopped'") as cur:
        assert [tuple(x) for x in await cur.fetchall()] == [("operator", 1)]


async def test_stop_refusals(db, client, live):
    eid = await security.raise_event(db, kind="login_failed", summary="burst")
    r = await client.post(f"/api/security/events/{eid}/stop", json={"scope": "agent"})
    assert r.status_code == 409 and "not tied to an agent run" in r.json()["detail"]
    await _conv(db, 30)
    ev = await _agent_event(db, 30)
    assert (await client.post(f"/api/security/events/{ev}/stop",
                              json={"scope": "everything"})).status_code == 409


async def test_stopping_a_run_also_stops_the_plan_runner_driving_it(db, client, live, monkeypatch):
    from backend import plan
    await _conv(db, 40)
    await _conv(db, 41, parent=40, kind="agent", job="j")
    live.ids = {41}
    cancelled = []
    runner = SimpleNamespace(done=lambda: False, cancel=lambda: cancelled.append("plan"))
    monkeypatch.setitem(plan._runs, "proj", runner)
    monkeypatch.setitem(plan._live_items, 41, {"project": "proj", "item_id": "i1", "title": "t"})
    eid = await _agent_event(db, 41)
    out = (await client.post(f"/api/security/events/{eid}/stop",
                             json={"scope": "run", "confirm": True})).json()
    assert out["plans"] == ["proj"] and cancelled == ["plan"] and live.stopped == [41]


def test_secactions_refusals_are_value_errors():
    assert issubclass(secactions.ActionRefused, ValueError)
