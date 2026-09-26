#!/usr/bin/env python3
"""Baked entrypoint of a Jav3 docker box (WP8). The docker counterpart of
vm/guest/bootstrap.py, kept as small and stable as that one.

  1. start the egress forwarder: a child process listening on 127.0.0.1:8443
     (the container's own loopback; the netns has no other interface) that
     splices each connection to /run/jav3/proxy.sock, the box's egress
     proxy on the host. HTTP(S)_PROXY point at it, so pip/npm/curl/git work
     the way they do in a KVM box, and the proxy is the only way out;
  2. report isolation to stdout (docker logs): interfaces, external reach;
  3. fetch the guest runtime package from the gateway (/run/jav3/gateway.sock),
     unpack it into /opt/jarvis (a tmpfs), exec the run-turn server with
     JAV3_TRANSPORT=unix.

Stdlib only; runs as uid 10001 with no capabilities.
"""
import base64
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import threading
import time

SOCK_DIR = "/run/jav3"                 # <box dir>/sock on the host
GATEWAY = f"{SOCK_DIR}/gateway.sock"
PROXY = f"{SOCK_DIR}/proxy.sock"
FWD_ADDR = ("127.0.0.1", 8443)
PROXY_URL = "http://127.0.0.1:8443"
DEST = "/opt/jarvis"


# --- the egress forwarder (child process) -------------------------------------

def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            b = src.recv(65536)
            if not b:
                break
            dst.sendall(b)
    except OSError:
        pass
    finally:
        for s, how in ((dst, socket.SHUT_WR), (src, socket.SHUT_RD)):
            try:
                s.shutdown(how)
            except OSError:
                pass


def _serve_one(client: socket.socket, proxy_path: str) -> None:
    up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        up.connect(proxy_path)
    except OSError:
        client.close()
        up.close()
        return
    t = threading.Thread(target=_pump, args=(up, client), daemon=True)
    t.start()
    _pump(client, up)
    t.join()
    client.close()
    up.close()


def forwarder(addr=FWD_ADDR, proxy_path: str = PROXY, ready=None) -> None:
    """TCP 127.0.0.1:8443 -> the host proxy's unix socket, one thread pair
    per connection. Loopback only: nothing outside the container's netns can
    reach it (and there is nothing outside it on the network anyway)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(addr)
    srv.listen(64)
    if ready is not None:
        ready(srv.getsockname())
    while True:
        c, _ = srv.accept()
        threading.Thread(target=_serve_one, args=(c, proxy_path), daemon=True).start()


# --- boot -------------------------------------------------------------------------

def report_isolation() -> None:
    try:
        ifaces = sorted(os.listdir("/sys/class/net"))
    except OSError:
        ifaces = []
    external = False
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3).close()
        external = True
    except OSError:
        pass
    print(f"GUEST-NET-IFACES: {ifaces}", flush=True)
    print(f"GUEST-NET-EXTERNAL-REACHABLE: {external}", flush=True)


def fetch_package(path: str = GATEWAY, tries: int = 60) -> bytes:
    for _ in range(tries):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(path)
            break
        except OSError:
            s.close()
            time.sleep(1)
    else:
        raise SystemExit("BOOTSTRAP: host gateway unreachable over unix socket")
    try:
        s.sendall((json.dumps({"op": "get_guest_package"}) + "\n").encode())
        line = s.makefile("rb").readline()
    finally:
        s.close()
    ev = json.loads(line)
    if ev.get("type") != "guest_package":
        raise SystemExit(f"BOOTSTRAP: unexpected reply: {str(ev)[:200]}")
    return base64.b64decode(ev["tar_b64"])


def proxy_env() -> dict:
    local = "localhost,127.0.0.1,::1"
    return {"JARVIS_EGRESS_PROXY": PROXY_URL, "HTTP_PROXY": PROXY_URL,
            "HTTPS_PROXY": PROXY_URL, "http_proxy": PROXY_URL,
            "https_proxy": PROXY_URL, "NO_PROXY": local, "no_proxy": local}


def main() -> None:
    subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "--forwarder"],
                     stdin=subprocess.DEVNULL)
    report_isolation()
    data = fetch_package()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(DEST, filter="data")
    print("BOOTSTRAP: guest package unpacked, starting run-turn server", flush=True)
    os.chdir(DEST)
    env = {**os.environ, **proxy_env(), "PYTHONPATH": DEST, "JAV3_TRANSPORT": "unix"}
    os.execve(sys.executable, [sys.executable, "-m", "backend.server"], env)


if __name__ == "__main__":
    if sys.argv[1:] == ["--forwarder"]:
        forwarder()
    else:
        try:
            main()
        except Exception as e:  # noqa: BLE001 — a boot failure must be visible in docker logs
            print(f"BOOTSTRAP-CRASH: {type(e).__name__}: {e}", flush=True)
            sys.exit(1)
