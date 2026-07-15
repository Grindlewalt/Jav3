#!/usr/bin/env python3
"""Baked guest bootstrap (the only guest code in the image; everything else is
pushed). On boot it: reports network isolation to the console, dials the host
gateway over vsock, fetches the current guest runtime package, unpacks it, and
execs the run-turn server. Kept minimal and stable so the image rarely changes.
"""
import base64
import io
import json
import os
import socket
import sys
import tarfile
import time

HOST_CID = socket.VMADDR_CID_HOST          # 2
GATEWAY_PORT = 5555
DEST = "/opt/jarvis"


def report_isolation() -> None:
    ifaces = sorted(os.listdir("/sys/class/net"))
    external = False
    try:
        c = socket.create_connection(("1.1.1.1", 53), timeout=3)
        c.close()
        external = True
    except OSError:
        external = False
    print(f"GUEST-NET-IFACES: {ifaces}", flush=True)
    print(f"GUEST-NET-EXTERNAL-REACHABLE: {external}", flush=True)


def fetch_package() -> bytes:
    for _ in range(40):
        try:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            s.connect((HOST_CID, GATEWAY_PORT))
            break
        except OSError:
            time.sleep(1)
    else:
        raise SystemExit("BOOTSTRAP: host gateway unreachable over vsock")
    try:
        s.sendall((json.dumps({"op": "get_guest_package"}) + "\n").encode())
        line = s.makefile("rb").readline()
    finally:
        s.close()
    ev = json.loads(line)
    if ev.get("type") != "guest_package":
        raise SystemExit(f"BOOTSTRAP: unexpected reply: {ev}")
    return base64.b64decode(ev["tar_b64"])


def main() -> None:
    report_isolation()
    data = fetch_package()
    os.makedirs(DEST, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(DEST, filter="data")
    print("BOOTSTRAP: guest package unpacked, starting run-turn server", flush=True)
    os.chdir(DEST)
    env = {**os.environ, "PYTHONPATH": DEST}
    os.execve(sys.executable, [sys.executable, "-m", "backend.server"], env)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — a boot failure must be visible on console
        print(f"BOOTSTRAP-CRASH: {type(e).__name__}: {e}", flush=True)
