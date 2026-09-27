"""Service boxes (WP3): backend/vm/services.py, services_api.py, portfwd.py,
svc_pkg.py, guest/svc/svcd.py and the /persist retirement.

No VM, QMP or svcd runs on the laptop: the box runtime (boxes.start/stop/
destroy/controller), QMP and the svcd RPC are stubbed. What is pinned is the
host-side policy: what a request may contain, that the host snapshots and
hashes the files and refuses secrets, the approve state machine with its
REQUIRED explicit placement and exposure, the exact hardened unit argv, the
relay's refusal to bind Jav3's own address, deny-by-default service egress,
and /persist's freeze + import + timed delete.
"""
import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import pathlib
import socket
import tarfile

import httpx
import pytest

from backend import secrets as secrets_mod
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds
from backend.vm import boxes, broker, persist, portfwd, services, svc_pkg

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_svcd():
    spec = importlib.util.spec_from_file_location("svcd", ROOT / "guest/svc/svcd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


svcd = _load_svcd()


# --- fixtures ---------------------------------------------------------------------------

class FakeCtl:
    def __init__(self):
        self.up = False
        self.pid = None
        self.booted_at = None
        self.inflight = 0

    def running(self):
        return self.up


class Runtime:
    """Stubs the WP1 runtime + QMP + svcd, recording what services.py did."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple] = []
        self.rpc: list[dict] = []
        self.qmp: list[dict] = []
        self.svcd_ok = True
        self.apply_errors: dict = {}
        self.units: dict = {}
        self.import_tar = b""
        self.log_text = ""

        def controller(box):
            if box.ctl is None:
                box.ctl = FakeCtl()
            return box.ctl

        async def start(box):
            self.calls.append(("start", box.id))
            box.ctl = box.ctl or FakeCtl()
            box.ctl.up = True

        async def stop(box):
            self.calls.append(("stop", box.id))
            if box.ctl:
                box.ctl.up = False

        async def destroy(box, delete_data=False):
            self.calls.append(("destroy", box.id))
            await stop(box)
            boxes.registry.release(box.id)

        async def rpc(box, req, timeout=30):
            self.rpc.append(req)
            if not self.svcd_ok:
                raise ConnectionError("down")
            op = req["op"]
            if op == "ping":
                return {"ok": True, "units": self.units}
            if op == "apply":
                return {"ok": True, "errors": self.apply_errors}
            if op == "import_read":
                return {"ok": True, "tar_b64": base64.b64encode(self.import_tar).decode()}
            if op == "logs":
                return {"ok": True, "text": self.log_text}
            return {"ok": True}

        async def qmp(box, cmds):
            self.qmp.extend(cmds)
            return [{"return": {}} for _ in cmds]

        async def ensure_disk(p):
            return True

        monkeypatch.setattr(boxes, "controller", controller)
        monkeypatch.setattr(boxes, "start", start)
        monkeypatch.setattr(boxes, "stop", stop)
        monkeypatch.setattr(boxes, "destroy", destroy)
        monkeypatch.setattr(services, "svcd_rpc", rpc)
        monkeypatch.setattr(services, "_qmp", qmp)
        monkeypatch.setattr(services, "ensure_data_disk", ensure_disk)

    def ops(self):
        return [r["op"] for r in self.rpc]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    boxes.registry.reset()
    services._reset_for_tests()
    monkeypatch.setattr(services, "_running", False)     # no background kicks
    yield
    boxes.registry.reset()
    services._reset_for_tests()


@pytest.fixture
async def env(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "services_lan_ip", "")
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.execute(
            "INSERT INTO security_profiles (name, builtin, allow_services,"
            " service_placement, box_runtime) VALUES ('Default', 1, 1,"
            " 'per_project', 'kvm')")
        await db.commit()
    finally:
        await db.close()
    return tmp_env


@pytest.fixture
async def client(env):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        await c.post("/api/projects", json={"name": "Demo", "summary": "a demo"})
        proj = settings.projects_dir / "demo"
        (proj / "app").mkdir(parents=True, exist_ok=True)
        (proj / "app" / "server.py").write_text("print('hi')\n")
        (proj / "app" / "util.py").write_text("X = 1\n")
        yield c


def _req(**kw):
    base = {"name": "api", "description": "a tiny api",
            "command": ["python3", "server.py", "--port", "8080"],
            "workdir": "app", "files": ["app/**"],
            "ports": [{"port": 8080, "purpose": "http", "expose": "lan"},
                      {"port": 9090, "purpose": "metrics", "expose": "host"}],
            "restart": "on-failure", "egress_hosts": ["api.example.com"],
            "env": {"MODE": "prod"}, "reason": "keep the api up"}
    base.update(kw)
    return base


async def _events(kind: str) -> list[dict]:
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM security_events WHERE kind = ?",
                              (kind,)) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


def _set_secret(name="TOKEN", value="sk-live-abcdef123456"):
    secrets_mod.save({name: {"value": value, "hosts": []}})
    return value


# --- request validation ---------------------------------------------------------------------

def test_validate_canonical(tmp_env):
    c = services.validate_request(_req(egress_hosts=["API.Example.com.", "b.io"]))
    assert c["command"] == ["python3", "server.py", "--port", "8080"]
    assert c["egress_hosts"] == ["api.example.com", "b.io"]
    assert [p["port"] for p in c["ports"]] == [8080, 9090]
    assert c["ports"][0] == {"port": 8080, "protocol": "tcp", "purpose": "http",
                             "expose": "lan"}


@pytest.mark.parametrize("patch,msg", [
    ({"command": "python3 server.py"}, "argv list"),
    ({"command": []}, "argv list"),
    ({"command": [" "]}, "argv[0]"),
    ({"command": ["sh", "-c", "a\x00b"]}, "printable"),
    ({"command": ["curl", "{{secret:TOKEN}}"]}, "placeholders"),
    ({"name": "Bad Name"}, "name"),
    ({"reason": ""}, "reason"),
    ({"files": []}, "files"),
    ({"files": ["../etc/passwd"]}, ".."),
    ({"files": ["/etc/passwd"]}, "relative"),
    ({"files": [".git/config"]}, ".git"),
    ({"workdir": "app/*"}, "glob"),
    ({"env": {"lower": "x"}}, "UPPER_SNAKE"),
    ({"env": {"LD_PRELOAD": "/x.so"}}, "reserved"),
    ({"env": {"HTTPS_PROXY": "http://evil"}}, "reserved"),
    ({"env": {"KEY": "{{secret:TOKEN}}"}}, "placeholders"),
    ({"env": {"KEY": 5}}, "printable string"),
    ({"ports": [{"port": 80, "purpose": "x"}]}, "1024..65535"),
    ({"ports": [{"port": 5558, "purpose": "x"}]}, "Jav3's own"),
    ({"ports": [{"port": 8080, "purpose": "x"}, {"port": 8080, "purpose": "y"}]}, "twice"),
    ({"ports": [{"port": 8080, "purpose": "x", "protocol": "udp"}]}, "tcp"),
    ({"ports": [{"port": 8080, "purpose": "x", "expose": "world"}]}, "expose"),
    ({"ports": [{"port": 8080}]}, "purpose"),
    ({"ports": [{"port": True, "purpose": "x"}]}, "1024..65535"),
    ({"restart": "sometimes"}, "restart"),
    ({"egress_hosts": ["10.0.0.1"]}, "IP address"),
    ({"egress_hosts": ["not a host"]}, "hostname"),
])
def test_validate_refuses(tmp_env, patch, msg):
    with pytest.raises(services.ServiceError) as e:
        services.validate_request(_req(**patch))
    assert msg in str(e.value)


def test_validate_refuses_secret_values_anywhere(tmp_env):
    val = _set_secret()
    for patch in ({"env": {"KEY": f"Bearer {val}"}},
                  {"command": ["curl", "-H", f"auth: {val}"]},
                  {"description": f"uses {val}"}):
        with pytest.raises(services.ServiceError) as e:
            services.validate_request(_req(**patch))
        assert "TOKEN" in str(e.value) and val not in str(e.value)


# --- snapshot ----------------------------------------------------------------------------------

def _proj(tmp_env):
    p = settings.projects_dir / "demo"
    (p / "app").mkdir(parents=True, exist_ok=True)
    (p / "app" / "server.py").write_text("print('hi')\n")
    (p / "app" / "util.py").write_text("X = 1\n")
    return p


def test_snapshot_is_deterministic_and_content_hashed(tmp_env):
    import os
    p = _proj(tmp_env)
    a, man = services.snapshot("demo", ["app/**"])
    os.utime(p / "app" / "server.py", (1, 1))          # mtime is not content
    b, _ = services.snapshot("demo", ["app/**"])
    assert a == b
    assert [m["path"] for m in man] == ["app/server.py", "app/util.py"]
    (p / "app" / "util.py").write_text("X = 2\n")
    c, _ = services.snapshot("demo", ["app/**"])
    assert hashlib.sha256(c).hexdigest() != hashlib.sha256(a).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(a)) as t:
        m = t.getmember("app/server.py")
        assert (m.mtime, m.uid, m.uname) == (0, 0, "")


def test_snapshot_refuses_secrets_symlinks_and_misses(tmp_env):
    p = _proj(tmp_env)
    val = _set_secret()
    (p / "app" / "cfg.py").write_text(f"KEY = '{val}'\n")
    with pytest.raises(services.ServiceError) as e:
        services.snapshot("demo", ["app/**"])
    assert "app/cfg.py" in str(e.value) and "TOKEN" in str(e.value)
    assert val not in str(e.value)
    (p / "app" / "cfg.py").unlink()
    (p / "app" / "link").symlink_to("/etc/hosts")
    with pytest.raises(services.ServiceError, match="symlink"):
        services.snapshot("demo", ["app/**"])
    (p / "app" / "link").unlink()
    with pytest.raises(services.ServiceError, match="matches nothing"):
        services.snapshot("demo", ["nope/*.py"])


def test_snapshot_skips_git(tmp_env):
    p = _proj(tmp_env)
    (p / "app" / ".git").mkdir()
    (p / "app" / ".git" / "config").write_text("x")
    _, man = services.snapshot("demo", ["app"])
    assert all(".git" not in m["path"] for m in man)


# --- filing ------------------------------------------------------------------------------------

async def test_file_request_snapshots_and_files(client):
    row = await services.file_request("demo", _req(), conversation_id=7)
    assert row["status"] == "pending" and row["placement"] == "per_project"
    assert row["desired_state"] == "stopped" and row["conversation_id"] == 7
    data = pathlib.Path(row["artifact_path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == row["artifact_sha256"]
    assert pathlib.Path(row["artifact_path"]).name == f"{row['artifact_sha256']}.tar"
    assert f"/svc/demo/{row['id']}/" in row["artifact_path"]
    ev = await _events("service_requested")
    assert len(ev) == 1 and ev[0]["severity"] == "info"
    with pytest.raises(services.ServiceError, match="already pending"):
        await services.file_request("demo", _req())


async def test_file_request_needs_profile_and_boxes(client, monkeypatch):
    db = await get_db()
    try:
        await db.execute("UPDATE security_profiles SET allow_services = 0")
        await db.commit()
    finally:
        await db.close()
    with pytest.raises(services.ServiceError, match="does not allow") as e:
        await services.file_request("demo", _req())
    assert e.value.status == 403
    monkeypatch.setattr(settings, "vm_boxes_enabled", False)
    with pytest.raises(services.ServiceError, match="vm_boxes_enabled"):
        await services.file_request("demo", _req())


async def test_tool_handler_files_and_reports_errors(client):
    from backend import runtime
    tok = runtime.active_project.set("demo")
    try:
        from tools.service_request.handler import run
        out = await run(**_req())
        assert "filed" in out and "Nothing runs" in out
        out = await run(**_req(env={"K": "{{secret:X}}"}))
        assert out.startswith("error:")
        from tools.service_status.handler import run as status
        assert "pending" in await status()
    finally:
        runtime.active_project.reset(tok)


# --- approve state machine -------------------------------------------------------------------

async def test_approve_requires_explicit_placement_and_exposure(client):
    row = await services.file_request("demo", _req())
    sid = row["id"]
    r = await client.post(f"/api/services/{sid}/approve",
                          json={"acknowledge": True, "expose_ports": []})
    assert r.status_code == 422                       # no placement
    r = await client.post(f"/api/services/{sid}/approve",
                          json={"acknowledge": True, "placement": "per_project"})
    assert r.status_code == 422                       # no expose_ports
    r = await client.post(f"/api/services/{sid}/approve",
                          json={"acknowledge": True, "placement": "anywhere",
                                "expose_ports": []})
    assert r.status_code == 422
    r = await client.post(f"/api/services/{sid}/approve",
                          json={"placement": "per_project", "expose_ports": []})
    assert r.status_code == 400                       # no acknowledge
    assert (await services.get(sid))["status"] == "pending"


async def test_approve_exposure_rules(client):
    sid = (await services.file_request("demo", _req()))["id"]
    ok = {"acknowledge": True, "placement": "per_service"}
    r = await client.post(f"/api/services/{sid}/approve",
                          json={**ok, "expose_ports": [{"port": 8080, "bind": "lan"}]})
    assert r.status_code == 409 and "services_lan_ip" in r.json()["detail"]
    r = await client.post(f"/api/services/{sid}/approve",
                          json={**ok, "expose_ports": [{"port": 9090, "bind": "lan"}]})
    assert r.status_code == 400 and "host only" in r.json()["detail"]
    r = await client.post(f"/api/services/{sid}/approve",
                          json={**ok, "expose_ports": [{"port": 7777, "bind": "loopback"}]})
    assert r.status_code == 400
    r = await client.post(f"/api/services/{sid}/approve",
                          json={**ok, "expose_ports": [{"port": 9090, "bind": "loopback"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved" and body["desired_state"] == "running"
    assert body["placement"] == "per_service"          # the operator's choice, stored
    assert body["expose_ports"] == [{"port": 9090, "bind": "loopback"}]
    assert body["box_id"] == f"s-demo-{sid}"
    ev = await _events("service_approved")
    assert len(ev) == 1 and json.loads(ev[0]["detail"])["placement"] == "per_service"
    r = await client.post(f"/api/services/{sid}/approve",
                          json={**ok, "expose_ports": []})
    assert r.status_code == 409                        # not pending any more
    r = await client.post(f"/api/services/{sid}/reject", json={"reason": "x"})
    assert r.status_code == 409


async def test_ui_bind_names_and_lan_ip_in_listing(client):
    body = (await client.get("/api/services?project=demo")).json()
    assert body["services_lan_ip"] == "" and body["services"] == []
    sid = (await services.file_request("demo", _req()))["id"]
    r = await client.post(f"/api/services/{sid}/approve",
                          json={"acknowledge": True, "placement": "per_project",
                                "expose_ports": [{"port": 9090, "bind": "host"},
                                                 {"port": 8080, "bind": "none"}]})
    assert r.status_code == 200, r.text
    assert r.json()["expose_ports"] == [{"port": 9090, "bind": "loopback"}]


async def test_approve_refuses_tampered_artifact(client):
    row = await services.file_request("demo", _req())
    p = pathlib.Path(row["artifact_path"])
    p.chmod(0o600)
    p.write_bytes(p.read_bytes() + b"\0")
    r = await client.post(f"/api/services/{row['id']}/approve",
                          json={"acknowledge": True, "placement": "per_project",
                                "expose_ports": []})
    assert r.status_code == 409 and "hash" in r.json()["detail"]


async def test_reject_and_cookie_only(client):
    sid = (await services.file_request("demo", _req()))["id"]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
        r = await anon.post(f"/api/services/{sid}/approve",
                            json={"acknowledge": True, "placement": "per_project",
                                  "expose_ports": []},
                            headers={"Authorization": "Bearer whatever"})
        assert r.status_code == 401
    r = await client.post(f"/api/services/{sid}/reject", json={"reason": "no"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert (await client.get("/api/services?project=demo")).json()["services"][0]["status"] == "rejected"


async def test_supersede_diff_and_replace(client):
    sid = (await services.file_request("demo", _req()))["id"]
    await services.approve(sid, placement="per_project", expose_ports=[])
    with pytest.raises(services.ServiceError, match="exactly this definition"):
        await services.file_request("demo", _req())
    (settings.projects_dir / "demo" / "app" / "util.py").write_text("X = 2\n")
    new = await services.file_request("demo", _req())
    assert new["supersedes_id"] == sid
    r = await client.get(f"/api/services/{new['id']}")
    d = r.json()["diff"]
    assert "-X = 1" in d and "+X = 2" in d and "artifact_sha256" in d
    await services.approve(new["id"], placement="per_project", expose_ports=[])
    assert (await services.get(sid))["status"] == "superseded"


async def test_revoke_destroys_the_box(client, monkeypatch):
    rt = Runtime(monkeypatch)
    sid = (await services.file_request("demo", _req()))["id"]
    await services.approve(sid, placement="per_project", expose_ports=[])
    await services.reconcile_box("s-demo")
    assert ("start", "s-demo") in rt.calls
    r = await client.post(f"/api/services/{sid}/revoke", json={"delete_data": True})
    assert r.status_code == 400                        # confirm required
    r = await client.post(f"/api/services/{sid}/revoke",
                          json={"confirm": True, "delete_data": True})
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert ("destroy", "s-demo") in rt.calls
    assert boxes.get("s-demo") is None
    assert len(await _events("service_revoked")) == 1


# --- reconcile / svcd / egress ----------------------------------------------------------------

async def test_reconcile_boots_fresh_and_applies(client, monkeypatch):
    rt = Runtime(monkeypatch)
    sid = (await services.file_request("demo", _req()))["id"]
    await services.approve(sid, placement="per_project", expose_ports=[])
    box_dir = settings.vm_dir / "boxes" / "s-demo"
    box_dir.mkdir(parents=True)
    (box_dir / "overlay.qcow2").write_bytes(b"old root")      # a planted root
    await services.reconcile_box("s-demo")
    assert not (box_dir / "overlay.qcow2").exists()           # root reset
    box = boxes.get("s-demo")
    assert box.kind == "service" and box.image[0] == services.SVC_VARIANT
    assert box.mem_mb == settings.vm_service_box_mem_mb
    assert rt.ops() == ["ping", "mount_srv", "apply"]
    assert any(c.get("arguments", {}).get("serial") == "jsrv" for c in rt.qmp)
    apply = rt.rpc[-1]
    (spec,) = apply["services"]
    assert spec["id"] == sid and spec["env"] == {"MODE": "prod"}
    assert base64.b64decode(spec["artifact_b64"]) == pathlib.Path(
        (await services.get(sid))["artifact_path"]).read_bytes()
    assert (await client.get(f"/api/services/{sid}")).json()["state"] == "running"
    # re-apply on a running box does not reboot it
    await services.reconcile_box("s-demo")
    assert [c for c in rt.calls if c[0] == "start"] == [("start", "s-demo")]


async def test_service_egress_is_deny_by_default(client, monkeypatch):
    Runtime(monkeypatch)
    sid = (await services.file_request("demo", _req()))["id"]
    await services.approve(sid, placement="per_project", expose_ports=[])
    await services.reconcile_box("s-demo")
    d = await services.egress_decide("s-demo", "api.example.com")
    assert d["allow"] and d["service_ids"] == [sid]
    assert (await services.egress_decide("s-demo", "v2.api.example.com"))["allow"]
    assert not (await services.egress_decide("s-demo", "example.com"))["allow"]
    assert not (await services.egress_decide("s-demo", "evil.com"))["allow"]
    assert not (await services.egress_decide("s-other", "api.example.com"))["allow"]
    db = await get_db()
    try:
        await db.execute("UPDATE security_profiles SET deny_hosts = ?",
                         (json.dumps(["example.com"]),))
        await db.commit()
    finally:
        await db.close()
    d = await services.egress_decide("s-demo", "api.example.com")
    assert not d["allow"] and "deny" in d["reason"]


async def test_unreported_box_alerts_once_and_restarts(client, monkeypatch):
    rt = Runtime(monkeypatch)
    sid = (await services.file_request("demo", _req()))["id"]
    await services.approve(sid, placement="per_project", expose_ports=[])
    await services.reconcile_box("s-demo")
    rt.svcd_ok = False
    spawned = []
    monkeypatch.setattr(services, "_spawn", lambda coro: spawned.append(coro))
    for _ in range(services._UNREPORTED_AFTER + 2):
        await services.check_box("s-demo")
    assert len(await _events("svc_unreported")) == 1
    assert ("stop", "s-demo") in rt.calls          # QEMU killed: fresh root next boot
    assert len(spawned) == 1                       # one restart; the rest back off
    for c in spawned:
        c.close()
    assert services.state_of(await services.get(sid)) in ("unreported", "failed")
    rt.svcd_ok = True
    rt.units = {str(sid): {"active": "active", "result": "success"}}
    await services.check_box("s-demo")
    assert services.state_of(await services.get(sid)) == "running"
    assert (await services.get(sid))["last_reported_at"]


async def test_services_topic_publishes_changes_not_pings(client, monkeypatch):
    from backend import bus, events_api
    assert "services" in events_api.TOPICS
    rt = Runtime(monkeypatch)
    q = bus.subscribe(services.BUS_CHAN)
    try:
        def drain():
            out = []
            while not q.empty():
                out.append(q.get_nowait())
            return out
        sid = (await services.file_request("demo", _req()))["id"]
        await services.approve(sid, placement="per_project", expose_ports=[])
        ev = drain()
        assert [(e["type"], e["status"]) for e in ev] == [("service_changed", "pending"),
                                                           ("service_changed", "approved")]
        assert ev[0]["service_id"] == sid and ev[0]["project"] == "demo"
        await services.reconcile_box("s-demo")
        assert {"type": "service_state", "service_id": sid, "state": "running",
                "error": None} in drain()
        rt.units = {str(sid): {"active": "active", "result": "success"}}
        await services.check_box("s-demo")            # same state: nothing published
        await services.check_box("s-demo")
        assert drain() == []
        rt.units = {str(sid): {"active": "failed", "result": "exit-code"}}
        await services.check_box("s-demo")
        assert [e["state"] for e in drain()] == ["failed"]
        await services.set_desired(sid, "stopped")
        assert [(e["type"], e["desired_state"]) for e in drain()
                if e["type"] == "service_changed"] == [("service_changed", "stopped")]
    finally:
        bus.unsubscribe(services.BUS_CHAN, q)


async def test_logs_are_scrubbed(client, monkeypatch):
    rt = Runtime(monkeypatch)
    val = _set_secret()
    sid = (await services.file_request("demo", _req()))["id"]
    await services.approve(sid, placement="per_project", expose_ports=[])
    await services.reconcile_box("s-demo")
    rt.log_text = f"GET / 200\nleaked {val}\n"
    r = await client.get(f"/api/services/{sid}/logs?lines=5")
    assert r.status_code == 200 and r.json()["untrusted"] is True
    assert val not in r.json()["text"] and "{{secret:TOKEN}}" in r.json()["text"]
    assert rt.rpc[-1] == {"op": "logs", "id": sid, "lines": 5}


def test_service_logs_is_untrusted():
    assert broker.classify_taint("service_logs") == "untrusted"
    assert broker.classify_taint("service_status") == "trusted"


def test_svc_package_is_svcd_only(tmp_env):
    data = svc_pkg.build_package_tar()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        names = set(t.getnames())
        server = t.extractfile("backend/server.py").read()
    assert {"backend/__init__.py", "backend/server.py"} <= names
    assert not any(n.startswith("tools/") or "loop" in n for n in names)
    assert server == (ROOT / "guest/svc/svcd.py").read_bytes()


# --- svcd unit generation (snapshot) -----------------------------------------------------------

def test_unit_argv_snapshot():
    spec = {"id": 7, "name": "api", "command": ["python3", "server.py", "$HOME"],
            "workdir": "app", "env": {"MODE": "prod", "SRV": "/nope"},
            "restart": "on-failure", "mem_mb": 256}
    argv = svcd.unit_argv(spec, proxy="http://10.201.50.1:8443", imported=True)
    P = "--property="
    assert argv == [
        "systemd-run", "--unit=jav3-svc-7", "--quiet", "--no-block",
        "--service-type=exec",
        P + "Description=jav3 service 7", P + "DynamicUser=yes",
        P + "User=jav3-svc-7", P + "StateDirectory=jav3-svc-7",
        P + "StateDirectoryMode=0700", P + "ProtectSystem=strict",
        P + "ProtectHome=yes", P + "PrivateTmp=yes", P + "PrivateDevices=yes",
        P + "NoNewPrivileges=yes", P + "RestrictSUIDSGID=yes",
        P + "ProtectKernelTunables=yes", P + "ProtectKernelModules=yes",
        P + "ProtectKernelLogs=yes", P + "ProtectControlGroups=yes",
        P + "ProtectClock=yes", P + "ProtectHostname=yes",
        P + "RestrictNamespaces=yes", P + "RestrictRealtime=yes",
        P + "LockPersonality=yes", P + "SystemCallArchitectures=native",
        P + "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        P + "CapabilityBoundingSet=", P + "AmbientCapabilities=",
        P + "UMask=0077", P + "InaccessiblePaths=/srv",
        P + "InaccessiblePaths=-/opt/jarvis", P + "MemoryMax=256M",
        P + "MemorySwapMax=0", P + "TasksMax=256",
        P + "WorkingDirectory=/opt/svc/7/app", P + "Restart=on-failure",
        P + "RestartSec=2",
        P + "BindReadOnlyPaths=/var/lib/private/_imported:/persist",
        "--setenv=HTTP_PROXY=http://10.201.50.1:8443",
        "--setenv=HTTPS_PROXY=http://10.201.50.1:8443",
        "--setenv=http_proxy=http://10.201.50.1:8443",
        "--setenv=https_proxy=http://10.201.50.1:8443",
        "--setenv=NO_PROXY=localhost,127.0.0.1",
        "--setenv=no_proxy=localhost,127.0.0.1",
        "--setenv=SRV=/var/lib/jav3-svc-7",
        "--setenv=MODE=prod",
        "--", "python3", "server.py", "$$HOME"]


def test_unit_argv_refusals():
    base = {"id": 1, "command": ["x"], "workdir": ".", "restart": "no"}
    argv = svcd.unit_argv(base, proxy="", imported=False)
    assert "--property=Restart=no" in argv
    assert not any("RestartSec" in a or "BindReadOnly" in a for a in argv)
    with pytest.raises(ValueError):
        svcd.unit_argv({**base, "workdir": "../../etc"}, proxy="", imported=False)
    with pytest.raises(ValueError):
        svcd.unit_argv({**base, "restart": "yes"}, proxy="", imported=False)
    with pytest.raises(ValueError):
        svcd.unit_argv({**base, "command": "sh -c x"}, proxy="", imported=False)


# --- relays: binding -----------------------------------------------------------------------------

def test_lan_ip_refusals(monkeypatch):
    monkeypatch.setattr(portfwd, "host_addresses",
                        lambda: [("127.0.0.1", "lo"), ("10.0.0.82", "eth0"),
                                 ("10.0.0.83", "eth0:jsvc"), ("10.0.0.90", "wlan0")])
    monkeypatch.setattr(portfwd, "_default_route_ip", lambda: "10.0.0.82")
    monkeypatch.setattr(settings, "csrf_allowed_hosts", ["10.0.0.91:8000"])
    for bad, why in (("", "not set"), ("127.0.0.1", "private"), ("0.0.0.0", "private"),
                     ("8.8.8.8", "private"), ("10.201.50.1", "guests"),
                     ("10.0.0.82", "Jav3's own"), ("10.0.0.90", "Jav3's own"),
                     ("10.0.0.91", "Jav3's own"), ("10.0.0.84", "dedicated"),
                     ("fe80::1", "private"), ("nonsense", "not an IP")):
        with pytest.raises(portfwd.PortfwdError, match=why):
            portfwd.check_lan_ip(bad)
    assert portfwd.check_lan_ip("10.0.0.83") == "10.0.0.83"
    monkeypatch.setattr(settings, "services_lan_ip", "10.0.0.82")
    with pytest.raises(portfwd.PortfwdError, match="Jav3's own"):
        portfwd.bind_address("lan")
    assert portfwd.bind_address("loopback") == "127.0.0.1"


def test_lan_ip_default_route_without_iproute(monkeypatch):
    monkeypatch.setattr(portfwd, "host_addresses", lambda: None)
    monkeypatch.setattr(portfwd, "_default_route_ip", lambda: "192.168.1.5")
    with pytest.raises(portfwd.PortfwdError, match="Jav3's own"):
        portfwd.check_lan_ip("192.168.1.5")


def test_ip_addr_parsing(monkeypatch):
    monkeypatch.setattr(portfwd, "_ip_addr_lines", lambda: [
        "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever",
        "2: eth0    inet 10.0.0.82/24 brd 10.0.0.255 scope global eth0\\       valid_lft",
        "2: eth0    inet 10.0.0.83/24 scope global secondary eth0:jsvc\\       valid_lft",
        "5: jsvc0    inet 10.0.0.84/24 scope global jsvc0\\       valid_lft"])
    assert portfwd.host_addresses() == [("127.0.0.1", "lo"), ("10.0.0.82", "eth0"),
                                        ("10.0.0.83", "eth0:jsvc"),
                                        ("10.0.0.84", "jsvc0")]
    assert "10.0.0.83" not in portfwd.jav3_addresses()
    assert "10.0.0.82" in portfwd.jav3_addresses()


async def test_approval_refuses_lan_on_jav3_address(client, monkeypatch):
    monkeypatch.setattr(portfwd, "host_addresses", lambda: [("10.0.0.82", "eth0")])
    monkeypatch.setattr(settings, "services_lan_ip", "10.0.0.82")
    sid = (await services.file_request("demo", _req()))["id"]
    r = await client.post(f"/api/services/{sid}/approve",
                          json={"acknowledge": True, "placement": "per_project",
                                "expose_ports": [{"port": 8080, "bind": "lan"}]})
    assert r.status_code == 409 and "Jav3's own" in r.json()["detail"]
    monkeypatch.setattr(portfwd, "host_addresses",
                        lambda: [("10.0.0.82", "eth0"), ("10.0.0.83", "eth0:jsvc")])
    monkeypatch.setattr(portfwd, "_default_route_ip", lambda: "10.0.0.82")
    monkeypatch.setattr(settings, "services_lan_ip", "10.0.0.83")
    r = await client.post(f"/api/services/{sid}/approve",
                          json={"acknowledge": True, "placement": "per_project",
                                "expose_ports": [{"port": 8080, "bind": "lan"}]})
    assert r.status_code == 200, r.text


async def test_relay_refuses_jav3_port(monkeypatch):
    monkeypatch.setattr(settings, "lan_port", 18123)
    r = portfwd.Relay(1, 18123, "loopback", "s-demo")
    with pytest.raises(portfwd.PortfwdError, match="Jav3's own"):
        await r.start()


async def test_loopback_relay_is_metered(client, monkeypatch):
    async def echo(reader, writer):
        data = await reader.read(100)
        writer.write(data.upper())
        await writer.drain()
        writer.close()
    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]

    async def tunnel(box, port):
        return await asyncio.open_connection("127.0.0.1", up_port)
    monkeypatch.setattr(services, "open_tunnel", tunnel)
    boxes.allocate("service", project="demo", placement="per_project",
                   variant="svc", mem_mb=384)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    await portfwd.sync_box("s-demo", [(1, port, "loopback")])
    (st,) = portfwd.status(1)
    assert st["listening"] and st["address"] == "127.0.0.1"
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"hello")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(100), 5) == b"HELLO"
    writer.close()
    for _ in range(50):
        if portfwd.status(1)[0]["bytes_in"]:
            break
        await asyncio.sleep(0.05)
    assert portfwd.status(1)[0]["bytes_in"] == 5
    assert portfwd.status(1)[0]["bytes_out"] == 5
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM service_port_events") as cur:
            (row,) = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert (row["bind"], row["bytes_in"], row["bytes_out"]) == ("loopback", 5, 5)
    assert row["closed_at"]
    await portfwd.sync_box("s-demo", [])
    assert portfwd.status(1) == []
    upstream.close()


# --- /persist retirement -------------------------------------------------------------------------

async def _approve_legacy(slug="demo"):
    db = await get_db()
    try:
        await db.execute("UPDATE projects SET persist_approved = 1 WHERE slug = ?", (slug,))
        await db.commit()
    finally:
        await db.close()


async def test_persist_new_approvals_frozen(client):
    r = await client.put("/api/projects/demo/persist",
                         json={"approved": True, "acknowledge": True})
    assert r.status_code == 409 and "retired" in r.json()["detail"]
    assert not await persist.approved("demo")
    await _approve_legacy()
    assert await persist.approved("demo")              # existing ones keep working
    r = await client.put("/api/projects/demo/persist", json={"approved": False})
    assert r.status_code == 200 and r.json()["approved"] is False
    assert r.json()["retired"] is True


async def test_persist_import_and_timed_delete(client, monkeypatch):
    rt = Runtime(monkeypatch)
    await _approve_legacy()
    r = await client.post("/api/projects/demo/persist/import", json={"confirm": True})
    assert r.status_code == 404                        # no disk yet
    persist.disk_dir().mkdir(parents=True, exist_ok=True)
    persist.disk_path("demo").write_bytes(b"qcow")
    assert (await client.post("/api/projects/demo/persist/import",
                              json={})).status_code == 400
    spawned = []
    monkeypatch.setattr(services, "_spawn", lambda coro: spawned.append(coro))
    r = await client.post("/api/projects/demo/persist/import", json={"confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved"] is False and body["imported_at"] and body["delete_after"]
    assert body["import"]["state"] == "pending"
    assert not await persist.approved("demo")          # frozen: never attaches again
    assert len(await _events("persist_imported")) == 1

    # the read phase: a fresh service box reads the old disk READ-ONLY first
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        ti = tarfile.TarInfo("notes.txt")
        ti.size = 2
        t.addfile(ti, io.BytesIO(b"hi"))
    rt.import_tar = buf.getvalue()
    await spawned[0]
    assert rt.ops()[:3] == ["ping", "import_read", "mount_srv"]
    imp = [c for c in rt.qmp if c.get("arguments", {}).get("serial") == "jimport"]
    assert imp and imp[0]["arguments"]["drive"] == "jimport"
    add = [c for c in rt.qmp if c["execute"] == "blockdev-add"
           and c["arguments"]["node-name"] == "jimport"]
    assert add[0]["arguments"]["read-only"] is True
    st = services.import_state("demo")
    assert st["state"] == "done" and st["sha256"] == hashlib.sha256(rt.import_tar).hexdigest()
    assert services.import_tar_path("demo").read_bytes() == rt.import_tar

    # every later apply to the project's box carries the import
    sid = (await services.file_request("demo", _req()))["id"]
    await services.approve(sid, placement="per_project", expose_ports=[])
    await services.reconcile_box("s-demo")
    assert rt.rpc[-1]["op"] == "apply"
    assert base64.b64decode(rt.rpc[-1]["import_tar_b64"]) == rt.import_tar

    # the old disk goes after persist_retire_days, not before
    assert await persist.sweep_retired() == []
    assert persist.disk_path("demo").exists()
    db = await get_db()
    try:
        await db.execute("UPDATE projects SET persist_delete_after = "
                         "datetime('now', '-1 minute') WHERE slug = 'demo'")
        await db.commit()
    finally:
        await db.close()
    assert await persist.sweep_retired() == ["demo"]
    assert not persist.disk_path("demo").exists()
    assert len(await _events("persist_disk_deleted")) == 1


async def test_import_refused_while_attached(client):
    persist.disk_dir().mkdir(parents=True, exist_ok=True)
    persist.disk_path("demo").write_bytes(b"qcow")
    persist._state.holder = "demo"
    try:
        r = await client.post("/api/projects/demo/persist/import", json={"confirm": True})
        assert r.status_code == 409
    finally:
        persist.forget()
