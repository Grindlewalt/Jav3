#!/usr/bin/env python3
"""Phase 2 guest self-test stub.

On boot the guest has NO network device — its only path off-box is an AF_VSOCK
channel to the host. This stub dials the host model gateway (CID 2), asks for one
completion, and prints the reply to the serial console (which QEMU captures to
console.log on the host). It proves the whole host<->guest model path end to end:
the guest can reason via the host, the DeepSeek key never enters the guest, and
the guest can reach nothing but the gateway.

Phase 3 replaces this stub with the real ReAct loop; the vsock protocol stays.
"""
import json
import os
import socket
import time

HOST_CID = socket.VMADDR_CID_HOST          # 2 — the host, from inside the guest
PORT = 5555                                 # must match settings.vm_vsock_port
CONNECT_RETRIES = 40                        # host gateway may bind just after boot


def report_isolation() -> None:
    """Prove the guest has no way off-box except vsock: list its network
    interfaces (should be loopback only) and confirm a plain internet connect
    fails (no NIC, no route). Printed to the console so the host can assert it."""
    ifaces = sorted(os.listdir("/sys/class/net"))
    external_reachable = False
    try:
        c = socket.create_connection(("1.1.1.1", 53), timeout=3)
        c.close()
        external_reachable = True
    except OSError:
        external_reachable = False
    print(f"GUEST-NET-IFACES: {ifaces}", flush=True)
    print(f"GUEST-NET-EXTERNAL-REACHABLE: {external_reachable}", flush=True)


def _connect() -> socket.socket:
    for _ in range(CONNECT_RETRIES):
        try:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            s.connect((HOST_CID, PORT))
            return s
        except OSError:
            time.sleep(1)
    raise SystemExit("GUEST-SELFTEST: could not reach host gateway over vsock")


def main() -> None:
    report_isolation()
    s = _connect()
    req = {
        "op": "model_call",
        "op_id": "vm-selftest",
        "messages": [
            {"role": "system", "content": "You are terse."},
            {"role": "user",
             "content": "Reply with exactly the word PONG and nothing else."},
        ],
    }
    s.sendall((json.dumps(req) + "\n").encode())

    reply = ""
    with s.makefile("rb") as f:
        for line in f:
            if not line.strip():
                continue
            ev = json.loads(line)
            kind = ev.get("type")
            if kind == "token":
                reply += ev.get("text", "")
            elif kind == "message":
                reply = ev.get("content") or reply
                print(f"GUEST-SELFTEST-REPLY: {reply.strip()!r}", flush=True)
                break
            elif kind == "error":
                print(f"GUEST-SELFTEST-ERROR: {ev.get('message')!r}", flush=True)
                break
    s.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — a stub; surface anything to the console
        print(f"GUEST-SELFTEST-CRASH: {type(e).__name__}: {e}", flush=True)
