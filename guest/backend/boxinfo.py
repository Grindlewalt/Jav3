"""Who this guest is, and how it reaches the host (docs/boxes-contract.md C).

The host ships `box.json` inside the guest package (next to `backend/`, at
/opt/jarvis/box.json) when multi-box mode is on. It names the box's addresses
and the transport: vsock for a KVM box, per-box AF_UNIX sockets under
/run/jav3 for a docker box. With no box.json (boxes off, or an older host)
everything here returns today's literals: vsock to CID 2, 10.201.0.2/24 via
10.201.0.1, so the shared guest behaves exactly as before.

This is the only guest module that knows which transport it is on; the
run-turn server, the model client and the tool broker client all go through
`gateway_connect()` / `listen()`.
"""
import json
import os
import socket
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "box.json"
HOST_CID = 2                                # socket.VMADDR_CID_HOST
DEFAULT_GATEWAY_PORT = 5555

_cache = None


def load() -> dict:
    """box.json, or {} when absent/unreadable (today's shared guest)."""
    global _cache
    if _cache is None:
        try:
            data = json.loads(_PATH.read_text())
            _cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def net() -> dict:
    """{guest_ip, prefix, gateway, dns, proxy}: box.json's, else the shared
    guest's fixed constants."""
    n = load().get("net") or {}
    return {"guest_ip": n.get("guest_ip") or "10.201.0.2",
            "prefix": int(n.get("prefix") or 24),
            "gateway": n.get("gateway") or "10.201.0.1",
            "dns": n.get("dns") or n.get("gateway") or "10.201.0.1",
            "proxy": n.get("proxy") or "http://10.201.0.1:8443"}


def kind() -> str:
    return load().get("kind") or "shared"


def unix_gateway() -> bool:
    """True on a docker box (the gateway is a unix socket, not vsock)."""
    return (load().get("gateway") or {}).get("transport") == "unix"


def gateway_connect(port: int | None = None) -> socket.socket:
    """A BLOCKING, connected socket to the host gateway. `port` overrides the
    vsock port (the turn spec's gateway_port); ignored on a unix transport."""
    g = load().get("gateway") or {}
    if g.get("transport") == "unix":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(g["path"])
        except BaseException:
            s.close()
            raise
        return s
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    try:
        s.connect((HOST_CID, port or g.get("port") or DEFAULT_GATEWAY_PORT))
    except BaseException:
        s.close()
        raise
    return s


def listen(name: str, port: int, backlog: int = 4) -> socket.socket:
    """A bound, listening, non-blocking server socket for `name` (runturn,
    shell, svcd): box.json's endpoint, else vsock ANY:`port`."""
    ep = (load().get("listen") or {}).get(name) or {}
    if ep.get("transport") == "unix":
        path = ep["path"]
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        os.chmod(path, 0o660)
    else:
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        s.bind((socket.VMADDR_CID_ANY, ep.get("port") or port))
    s.listen(backlog)
    s.setblocking(False)
    return s
