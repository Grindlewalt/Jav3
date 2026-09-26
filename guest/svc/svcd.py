#!/usr/bin/env python3
"""svcd: the service box's supervisor (DESIGN-BOXES.md (a), WP3).

Shipped as `backend/server.py` of the service package (backend/vm/svc_pkg.py),
so the baked bootstrap execs it exactly like the turn server. Stdlib only.
Runs as root in a SERVICE box: no workspace, no op tokens, no model or broker
access (the host gateway refuses those ops from a service box's CID).

The host is the only caller: it dials this server over the box transport
(vsock port 5558 from CID 2, or /run/jav3/5558.sock for a docker box) and
re-applies the approved definitions on every boot. Nothing about which
services run is stored in the guest: the root disk is a fresh overlay each
boot, and only /srv (a separate, capped, nodev,nosuid,noexec disk) persists.

Ops (one NDJSON request, one NDJSON reply per connection):

  ping                        -> {ok, units:{id:{active,sub,result,pid,restarts}}, srv}
  mount_srv {fresh}           mount /dev/disk/by-id/virtio-jsrv at /srv (mkfs only
                              when fresh AND blank), bind /srv/state at /var/lib/private
  import_read {max_bytes}     tar the READ-ONLY virtio-jimport disk (old /persist)
                              back to the host
  apply {services, wipe, import_tar_b64?, import_sha256?}
                              make the running set exactly `services`
  logs {id, lines}            journal tail of one unit
  tunnel {port}               then raw bytes <-> 127.0.0.1:<port>, only for a port
                              the host exposed in the last apply

Each service runs as a transient systemd unit `jav3-svc-<id>` (unit_argv):
DynamicUser, ProtectSystem=strict, NoNewPrivileges, PrivateTmp/Devices, no
capabilities, INET/UNIX sockets only (no AF_VSOCK: a service cannot reach
this server or the gateway), a memory cap, its code unpacked read-only at
/opt/svc/<id>, and its ONLY writable persistent place its StateDirectory
(/var/lib/jav3-svc-<id>, which lives on /srv/state).
"""
import base64
import hashlib
import io
import json
import os
import posixpath
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time

BOX_JSON = "/opt/jarvis/box.json"
SVC_ROOT = "/opt/svc"
SRV = "/srv"
STATE_BIND = "/var/lib/private"      # systemd's home for DynamicUser StateDirectory
IMPORT_DIR = STATE_BIND + "/_imported"
IMPORT_MNT = "/mnt/jimport"
SRV_DEV = "/dev/disk/by-id/virtio-jsrv"
IMPORT_DEV = "/dev/disk/by-id/virtio-jimport"
SRV_OPTS = "nodev,nosuid,noexec"
HOST_CID = 2
DEFAULT_PORT = 5558
LOG_CAP = 64 * 1024
REPORT_SECONDS = 15

_lock = threading.Lock()
_state = {"ids": [], "exposed": set(), "proxy": "", "srv": False}


def log(msg: str) -> None:
    print(f"SVCD: {msg}", flush=True)


def load_box() -> dict:
    try:
        with open(BOX_JSON) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# --- unit generation (pure; snapshot-tested on the host) --------------------------------

def unit_name(sid: int) -> str:
    return f"jav3-svc-{int(sid)}"


def workdir_of(spec: dict) -> str:
    root = f"{SVC_ROOT}/{int(spec['id'])}"
    wd = posixpath.normpath(posixpath.join(root, spec.get("workdir") or "."))
    if wd != root and not wd.startswith(root + "/"):
        raise ValueError("workdir escapes the service's code directory")
    return wd


def _no_expand(arg: str) -> str:
    # systemd substitutes $VAR / ${VAR} in ExecStart arguments at exec time;
    # "$$" is a literal "$". The argv the operator approved is run verbatim.
    return arg.replace("$", "$$")


def unit_argv(spec: dict, *, proxy: str, imported: bool) -> list[str]:
    """The exact systemd-run argv for one approved service."""
    sid = int(spec["id"])
    unit = unit_name(sid)
    mem = max(32, int(spec.get("mem_mb") or 256))
    restart = spec.get("restart") or "no"
    if restart not in ("no", "on-failure", "always"):
        raise ValueError("bad restart policy")
    cmd = spec.get("command")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
        raise ValueError("command must be an argv list")
    props = [
        f"Description=jav3 service {sid}",
        "DynamicUser=yes",
        f"User={unit}",
        f"StateDirectory={unit}",
        "StateDirectoryMode=0700",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "RestrictNamespaces=yes",
        "RestrictRealtime=yes",
        "LockPersonality=yes",
        "SystemCallArchitectures=native",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "UMask=0077",
        "InaccessiblePaths=/srv",
        "InaccessiblePaths=-/opt/jarvis",
        f"MemoryMax={mem}M",
        "MemorySwapMax=0",
        "TasksMax=256",
        f"WorkingDirectory={workdir_of(spec)}",
        f"Restart={restart}",
    ]
    if restart != "no":
        props.append("RestartSec=2")
    if imported:
        props.append(f"BindReadOnlyPaths={IMPORT_DIR}:/persist")
    env = {}
    if proxy:
        env.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
                    "http_proxy": proxy, "https_proxy": proxy,
                    "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"})
    env["SRV"] = f"/var/lib/{unit}"
    user_env = spec.get("env") or {}
    for k in sorted(user_env):
        if k not in env:
            env[k] = str(user_env[k])
    argv = ["systemd-run", f"--unit={unit}", "--quiet", "--no-block",
            "--service-type=exec"]
    argv += [f"--property={p}" for p in props]
    argv += [f"--setenv={k}={env[k]}" for k in env]
    argv += ["--", *[_no_expand(a) for a in cmd]]
    return argv


# --- helpers ------------------------------------------------------------------------------------

def run(argv, timeout=60, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kw)


def wait_dev(path: str, seconds: float = 20.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.25)
    return False


def safe_extract(data: bytes, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def make_readonly(path: str) -> None:
    """root-owned, world-readable, nobody-writable (exec bits kept)."""
    for root, _dirs, files in os.walk(path):
        os.chown(root, 0, 0)
        os.chmod(root, 0o555)
        for n in files:
            p = os.path.join(root, n)
            if os.path.islink(p):
                continue
            ex = os.stat(p).st_mode & 0o111
            os.chown(p, 0, 0)
            os.chmod(p, 0o555 if ex else 0o444)


# --- ops ------------------------------------------------------------------------------------

def op_mount_srv(req: dict) -> dict:
    if os.path.ismount(SRV):
        return {"ok": True, "already": True}
    if not wait_dev(SRV_DEV):
        return {"ok": False, "error": "no /srv device"}
    has_fs = run(["blkid", SRV_DEV]).returncode == 0
    if not has_fs:
        if not req.get("fresh"):
            # never format over data the host says has been mounted before
            return {"ok": False, "error": "no filesystem on a used /srv disk"}
        r = run(["mkfs.ext4", "-q", "-L", "jsrv", SRV_DEV], timeout=300)
        if r.returncode:
            return {"ok": False, "error": f"mkfs: {r.stderr[-200:]}"}
    os.makedirs(SRV, exist_ok=True)
    r = run(["mount", "-o", SRV_OPTS, SRV_DEV, SRV])
    if r.returncode:
        return {"ok": False, "error": f"mount: {r.stderr[-200:]}"}
    state = f"{SRV}/state"
    os.makedirs(state, exist_ok=True)
    os.chmod(state, 0o700)
    os.makedirs(STATE_BIND, exist_ok=True)
    os.chmod(STATE_BIND, 0o700)
    for argv in (["mount", "--bind", state, STATE_BIND],
                 ["mount", "-o", f"remount,bind,{SRV_OPTS}", STATE_BIND]):
        r = run(argv)
        if r.returncode:
            return {"ok": False, "error": f"bind: {r.stderr[-200:]}"}
    _state["srv"] = True
    return {"ok": True}


def op_import_read(req: dict) -> dict:
    cap = int(req.get("max_bytes") or 0)
    if not wait_dev(IMPORT_DEV):
        return {"ok": False, "error": "no import device"}
    os.makedirs(IMPORT_MNT, exist_ok=True)
    r = run(["mount", "-o", "ro,noload,nodev,nosuid,noexec", IMPORT_DEV, IMPORT_MNT])
    if r.returncode:
        return {"ok": False, "error": f"mount: {r.stderr[-200:]}"}
    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name in sorted(os.listdir(IMPORT_MNT)):
                if name == "lost+found":
                    continue
                tar.add(os.path.join(IMPORT_MNT, name), arcname=name)
                if cap and buf.tell() > cap:
                    return {"ok": False, "error": "over the cap"}
        return {"ok": True, "tar_b64": base64.b64encode(buf.getvalue()).decode()}
    finally:
        run(["umount", IMPORT_MNT])


def _unit_show(sid: int) -> dict:
    r = run(["systemctl", "show", unit_name(sid), "-p", "ActiveState", "-p", "SubState",
             "-p", "Result", "-p", "MainPID", "-p", "NRestarts"], timeout=10)
    kv = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    return {"active": kv.get("ActiveState"), "sub": kv.get("SubState"),
            "result": kv.get("Result"), "pid": int(kv.get("MainPID") or 0),
            "restarts": int(kv.get("NRestarts") or 0)}


def _known_units() -> list[int]:
    r = run(["systemctl", "list-units", "jav3-svc-*", "--all", "--plain",
             "--no-legend"], timeout=10)
    ids = []
    for line in r.stdout.splitlines():
        name = line.split()[0] if line.split() else ""
        if name.startswith("jav3-svc-") and name.endswith(".service"):
            try:
                ids.append(int(name[len("jav3-svc-"):-len(".service")]))
            except ValueError:
                pass
    return ids


def op_ping(req: dict) -> dict:
    with _lock:
        ids = list(_state["ids"])
    return {"ok": True, "v": 1, "srv": {"mounted": os.path.ismount(SRV)},
            "units": {str(i): _unit_show(i) for i in ids}}


def _stop(sid: int) -> None:
    run(["systemctl", "stop", unit_name(sid)], timeout=60)
    run(["systemctl", "reset-failed", unit_name(sid)], timeout=10)


def _unpack(spec: dict, data: bytes) -> str:
    sid = int(spec["id"])
    sha = hashlib.sha256(data).hexdigest()
    if sha != spec.get("artifact_sha256"):
        raise ValueError("artifact hash mismatch")
    dest = f"{SVC_ROOT}/{sid}"
    marker = f"{SVC_ROOT}/.{sid}.sha256"
    try:
        with open(marker) as f:
            if f.read().strip() == sha and os.path.isdir(dest):
                return dest
    except OSError:
        pass
    shutil.rmtree(dest, ignore_errors=True)
    safe_extract(data, dest)
    make_readonly(dest)
    with open(marker, "w") as f:
        f.write(sha)
    return dest


def _import(req: dict) -> bool:
    """Extract the host-held /persist import once per content hash."""
    data_b64, sha = req.get("import_tar_b64"), req.get("import_sha256")
    if not data_b64 or not sha or not os.path.ismount(SRV):
        return os.path.isdir(IMPORT_DIR)
    marker = f"{SRV}/.persist-imported"
    try:
        with open(marker) as f:
            if f.read().strip() == sha and os.path.isdir(IMPORT_DIR):
                return True
    except OSError:
        pass
    data = base64.b64decode(data_b64)
    if hashlib.sha256(data).hexdigest() != sha:
        return os.path.isdir(IMPORT_DIR)
    shutil.rmtree(IMPORT_DIR, ignore_errors=True)
    safe_extract(data, IMPORT_DIR)
    make_readonly(IMPORT_DIR)
    with open(marker, "w") as f:
        f.write(sha)
    return True


def op_apply(req: dict) -> dict:
    services = req.get("services") or []
    box = load_box()
    proxy = ((box.get("net") or {}).get("proxy")) or ""
    errors: dict[str, str] = {}
    want = {}
    for s in services:
        try:
            want[int(s["id"])] = s
        except (KeyError, TypeError, ValueError):
            continue
    for sid in req.get("wipe") or []:
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            continue
        if sid not in want:
            _stop(sid)
            shutil.rmtree(f"{STATE_BIND}/{unit_name(sid)}", ignore_errors=True)
    for sid in _known_units():
        if sid not in want:
            _stop(sid)
    imported = _import(req)
    exposed = set()
    for sid, s in want.items():
        try:
            _unpack(s, base64.b64decode(s.get("artifact_b64") or ""))
            st = _unit_show(sid)
            if st["active"] in ("active", "activating", "reloading"):
                exposed.update(int(p) for p in s.get("expose") or [])
                continue
            run(["systemctl", "reset-failed", unit_name(sid)], timeout=10)
            argv = unit_argv(s, proxy=proxy, imported=imported)
            r = run(argv, cwd=workdir_of(s))
            if r.returncode:
                errors[str(sid)] = (r.stderr or r.stdout)[-300:]
                continue
            exposed.update(int(p) for p in s.get("expose") or [])
        except Exception as e:  # noqa: BLE001 — one bad service must not stop the rest
            errors[str(sid)] = f"{type(e).__name__}: {e}"[:300]
    with _lock:
        _state["ids"] = sorted(want)
        _state["exposed"] = exposed
    return {"ok": True, "errors": errors}


def op_logs(req: dict) -> dict:
    sid = int(req.get("id"))
    lines = max(1, min(int(req.get("lines") or 100), 500))
    r = run(["journalctl", "-u", unit_name(sid), "-n", str(lines), "--no-pager",
             "-o", "short-iso"], timeout=15)
    return {"ok": True, "text": r.stdout[-LOG_CAP:]}


OPS = {"ping": op_ping, "mount_srv": op_mount_srv, "import_read": op_import_read,
       "apply": op_apply, "logs": op_logs}


# --- server ------------------------------------------------------------------------------------

def _pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _readline(conn: socket.socket, cap: int = 1 << 27) -> bytes:
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = conn.recv(1 << 16)
        if not chunk:
            break
        buf += chunk
        if len(buf) > cap:
            raise ValueError("request too large")
    return bytes(buf)


def handle(conn: socket.socket) -> None:
    try:
        req = json.loads(_readline(conn) or b"{}")
        op = req.get("op") if isinstance(req, dict) else None
        if op == "tunnel":
            port = int(req.get("port"))
            with _lock:
                ok = port in _state["exposed"]
            if not ok:
                conn.sendall(b'{"ok": false, "error": "port not exposed"}\n')
                return
            up = socket.create_connection(("127.0.0.1", port), timeout=10)
            up.settimeout(None)
            conn.sendall(b'{"ok": true}\n')
            t = threading.Thread(target=_pipe, args=(up, conn), daemon=True)
            t.start()
            _pipe(conn, up)
            t.join()
            up.close()
            return
        fn = OPS.get(op)
        out = fn(req) if fn else {"ok": False, "error": f"unknown op {op!r}"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    try:
        conn.sendall((json.dumps(out) + "\n").encode())
    except OSError:
        pass


def _serve_conn(conn: socket.socket) -> None:
    try:
        handle(conn)
    finally:
        conn.close()


def listen_socket(box: dict) -> socket.socket:
    spec = ((box.get("listen") or {}).get("svcd")) or {"transport": "vsock",
                                                       "port": DEFAULT_PORT}
    if spec.get("transport") == "unix":
        path = spec["path"]
        try:
            os.unlink(path)
        except OSError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)           # root-only: a service (non-root) cannot connect
        try:
            s.bind(path)
        finally:
            os.umask(old)
    else:
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        s.bind((socket.VMADDR_CID_ANY, int(spec.get("port") or DEFAULT_PORT)))
    s.listen(16)
    return s


def configure_network(box: dict) -> None:
    net = box.get("net") or {}
    ip, prefix, gw = net.get("guest_ip"), net.get("prefix"), net.get("gateway")
    if not ip or not gw:
        return
    ifaces = [i for i in sorted(os.listdir("/sys/class/net")) if i != "lo"]
    if not ifaces:
        return
    dev = ifaces[0]
    for argv in (["ip", "link", "set", dev, "up"],
                 ["ip", "addr", "replace", f"{ip}/{prefix}", "dev", dev],
                 ["ip", "route", "replace", "default", "via", gw]):
        run(argv)
    if net.get("dns"):
        try:
            with open("/etc/resolv.conf", "w") as f:
                f.write(f"nameserver {net['dns']}\n")
        except OSError:
            pass


def report_loop(box: dict) -> None:
    """Push svc_report through the gateway (the host also polls; either
    keeps svc_unreported quiet). Identity is the connection, not the body."""
    gw = box.get("gateway") or {"transport": "vsock", "cid": HOST_CID, "port": 5555}
    while True:
        time.sleep(REPORT_SECONDS)
        try:
            if gw.get("transport") == "unix":
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(gw["path"])
            else:
                s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
                s.connect((HOST_CID, int(gw.get("port") or 5555)))
            s.settimeout(10)
            with _lock:
                ids = list(_state["ids"])
            s.sendall((json.dumps({"op": "svc_report", "ids": ids}) + "\n").encode())
            _readline(s, 1 << 16)
            s.close()
        except OSError:
            pass


def main() -> None:
    box = load_box()
    configure_network(box)
    os.makedirs(SVC_ROOT, exist_ok=True)
    srv = listen_socket(box)
    log(f"listening ({box.get('id', '?')})")
    threading.Thread(target=report_loop, args=(box,), daemon=True).start()
    vsock = srv.family == getattr(socket, "AF_VSOCK", -1)
    while True:
        conn, peer = srv.accept()
        if vsock and (not isinstance(peer, tuple) or peer[0] != HOST_CID):
            conn.close()                 # only the host may drive svcd
            continue
        threading.Thread(target=_serve_conn, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — visible on the console
        print(f"SVCD-CRASH: {type(e).__name__}: {e}", flush=True)
        sys.exit(1)
