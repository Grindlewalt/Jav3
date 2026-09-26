"""Inbound relays for approved service ports (DESIGN-BOXES.md (a), WP3).

Nothing reaches a service box from outside by default. When the operator
exposes a port at approval, the host runs a relay here:

    client -> <bind address>:<port> (host) -> svcd tunnel (box transport) -> 127.0.0.1:<port> (box)

The relay never opens a network path into the box: it rides the same host-
dialed vsock/unix channel as every other host->box call, and svcd only tunnels
to ports the host exposed in its last apply. Every connection is metered
(bytes each way, peer) into `service_port_events`: the host-truth inbound
numbers the process view joins on.

Where it binds (operator decision 0.4):

  loopback  127.0.0.1 only.
  lan       ONLY `settings.services_lan_ip`, a dedicated second LAN address
            (vm/net/services_lan_ip.sh adds it). Empty = LAN exposure is
            unavailable. It must never be an address Jav3's own UI is served
            on: cookies ignore the port (residual #13), so a hostile service
            on Jav3's own address would receive the operator's session
            cookie. `lan_address()` refuses loopback/wildcard/non-private
            addresses, the egress /16, and every address Jav3 answers on
            (every other host address, the default-route source address, IP
            literals in csrf_allowed_hosts). Where iproute2 exists it must
            also carry the script's label (<iface>:jsvc, or be on jsvc0):
            proof it is the dedicated alias. Checked at approval AND at bind.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket

from ..config import settings

MAX_CONNS = 32                     # concurrent connections per relay
IDLE_TIMEOUT = 600.0               # seconds without a byte either way
_CHUNK = 65536


class PortfwdError(Exception):
    pass


# --- where a relay may bind -----------------------------------------------------------

def _default_route_ip() -> str | None:
    """The source address this host uses toward the LAN (UDP connect sends
    no packet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


SVC_LABELS = ("jsvc0",)            # macvlan interface services_lan_ip.sh creates
SVC_LABEL_SUFFIX = ":jsvc"         # alias label services_lan_ip.sh sets


def _ip_addr_lines() -> list[str] | None:
    """`ip -o -4 addr show` lines, or None where iproute2 is missing (macOS)."""
    import shutil
    import subprocess
    if not shutil.which("ip"):
        return None
    try:
        return subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True,
                              text=True, timeout=5).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None


def host_addresses() -> list[tuple[str, str]] | None:
    """(address, label) for every IPv4 address on the host, or None."""
    lines = _ip_addr_lines()
    if lines is None:
        return None
    out = []
    for line in lines:
        toks = line.split("\\", 1)[0].split()
        if "inet" not in toks:
            continue
        addr = toks[toks.index("inet") + 1].split("/", 1)[0]
        out.append((addr, toks[-1]))
    return out


def _is_svc_label(label: str) -> bool:
    return label in SVC_LABELS or label.endswith(SVC_LABEL_SUFFIX)


def jav3_addresses() -> set[str]:
    """Every address Jav3's own UI can be reached on (as far as the host can
    tell): every host address that is NOT the dedicated services alias, the
    default-route source, and IP literals the operator listed as Jav3's. A
    LAN relay may bind none of them."""
    out: set[str] = set()
    addrs = host_addresses()
    if addrs is not None:
        out.update(a for a, label in addrs if not _is_svc_label(label))
    else:
        try:
            from .. import lan
            out.update(lan.lan_ips())
        except Exception:  # noqa: BLE001 — best effort; the checks below remain
            pass
    ip = _default_route_ip()
    if ip:
        out.add(ip)
    for h in settings.csrf_allowed_hosts or []:
        host = str(h).rsplit(":", 1)[0].strip("[]")
        try:
            out.add(str(ipaddress.ip_address(host)))
        except ValueError:
            pass
    return out


def check_lan_ip(value: str) -> str:
    """`value` if it is usable as the dedicated services LAN address."""
    value = (value or "").strip()
    if not value:
        raise PortfwdError("LAN exposure is unavailable: services_lan_ip is not "
                           "set (add a second LAN address with "
                           "vm/net/services_lan_ip.sh, then set it)")
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise PortfwdError(f"services_lan_ip {value!r} is not an IP address") from None
    if ip.version != 4 or ip.is_loopback or ip.is_unspecified or ip.is_multicast \
            or ip.is_link_local or not ip.is_private:
        raise PortfwdError(f"services_lan_ip {value} must be a private, "
                           "non-loopback IPv4 LAN address")
    if ip in ipaddress.ip_network("10.201.0.0/16"):
        raise PortfwdError(f"services_lan_ip {value} is inside the guests' network")
    if value in jav3_addresses():
        raise PortfwdError(f"services_lan_ip {value} is an address Jav3's own UI "
                           "is served on; a service there would receive the "
                           "operator's session cookie. Use a dedicated second "
                           "address")
    addrs = host_addresses()
    if addrs is not None and not any(a == value and _is_svc_label(lb)
                                     for a, lb in addrs):
        # where the host can tell, the address must be the one the script
        # made (label eth0:jsvc or interface jsvc0): proof it is dedicated
        raise PortfwdError(f"services_lan_ip {value} is not the dedicated services "
                           "address on this host (add it with "
                           "vm/net/services_lan_ip.sh add)")
    return value


def lan_address() -> str:
    return check_lan_ip(settings.services_lan_ip)


def bind_address(bind: str) -> str:
    if bind == "loopback":
        return "127.0.0.1"
    if bind == "lan":
        return lan_address()
    raise PortfwdError(f"unknown bind {bind!r}")


# --- relays ------------------------------------------------------------------------------

class Relay:
    def __init__(self, service_id: int, port: int, bind: str, box_id: str):
        self.service_id, self.port, self.bind, self.box_id = service_id, port, bind, box_id
        self.server: asyncio.base_events.Server | None = None
        self.address: str | None = None
        self.conns = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.error: str | None = None

    @property
    def key(self) -> tuple:
        return (self.service_id, self.port, self.bind)

    async def start(self) -> None:
        self.address = bind_address(self.bind)       # re-checked at every bind
        if self.port == settings.lan_port:
            raise PortfwdError(f"port {self.port} is Jav3's own")
        self.server = await asyncio.start_server(self._handle, self.address, self.port,
                                                 reuse_address=True)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 5)
            except asyncio.TimeoutError:
                pass
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        from . import boxes, services
        peer = writer.get_extra_info("peername")
        peer_s = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)
        if self.conns >= MAX_CONNS:
            writer.close()
            return
        box = boxes.get(self.box_id)
        if box is None:
            writer.close()
            return
        self.conns += 1
        ev = await _open_event(self, peer_s)
        counts = [0, 0]                       # in (client->service), out
        try:
            try:
                br, bw = await services.open_tunnel(box, self.port)
            except (OSError, ConnectionError, asyncio.TimeoutError) as e:
                self.error = f"tunnel: {type(e).__name__}"
                return

            async def pipe(src, dst, i):
                try:
                    while True:
                        data = await asyncio.wait_for(src.read(_CHUNK), IDLE_TIMEOUT)
                        if not data:
                            break
                        counts[i] += len(data)
                        dst.write(data)
                        await dst.drain()
                except (OSError, ConnectionError, asyncio.TimeoutError):
                    pass
                finally:
                    try:
                        dst.close()
                    except Exception:  # noqa: BLE001
                        pass
            await asyncio.gather(pipe(reader, bw, 0), pipe(br, writer, 1))
        finally:
            self.conns -= 1
            self.bytes_in += counts[0]
            self.bytes_out += counts[1]
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            await _close_event(ev, counts[0], counts[1])

    def to_json(self) -> dict:
        return {"service_id": self.service_id, "port": self.port, "bind": self.bind,
                "address": self.address, "box_id": self.box_id,
                "listening": self.server is not None, "conns": self.conns,
                "bytes_in": self.bytes_in, "bytes_out": self.bytes_out,
                "error": self.error}


async def _open_event(r: Relay, peer: str) -> int | None:
    from ..db import get_db
    try:
        db = await get_db()
        try:
            cur = await db.execute(
                "INSERT INTO service_port_events (service_id, box_id, port, bind, peer)"
                " VALUES (?,?,?,?,?)", (r.service_id, r.box_id, r.port, r.bind, peer))
            await db.commit()
            return cur.lastrowid
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — metering must not break the relay
        return None


async def _close_event(ev: int | None, bytes_in: int, bytes_out: int) -> None:
    if ev is None:
        return
    from ..db import get_db
    try:
        db = await get_db()
        try:
            await db.execute(
                "UPDATE service_port_events SET bytes_in = ?, bytes_out = ?,"
                " closed_at = datetime('now') WHERE id = ?", (bytes_in, bytes_out, ev))
            await db.commit()
        finally:
            await db.close()
    except Exception:  # noqa: BLE001
        pass


_relays: dict[str, dict[tuple, Relay]] = {}      # box id -> key -> relay


async def sync_box(box_id: str, want: list[tuple[int, int, str]]) -> None:
    """Make the box's relays exactly `want` [(service_id, port, bind)]."""
    have = _relays.setdefault(box_id, {})
    keys = {tuple(w) for w in want}
    for k in [k for k in have if k not in keys]:
        await have.pop(k).stop()
    for sid, port, bind in sorted(keys):
        r = have.get((sid, port, bind))
        if r is not None and r.server is not None:
            continue
        r = r or Relay(sid, port, bind, box_id)       # a failed bind is retried
        try:
            await r.start()
            r.error = None
        except (PortfwdError, OSError) as e:
            r.error = str(e)[:300]
            print(f"[portfwd] {box_id} {port}/{bind}: {r.error}")
        have[(sid, port, bind)] = r
    if not have:
        _relays.pop(box_id, None)


async def close_service(service_id: int) -> None:
    for box_id, have in list(_relays.items()):
        for k in [k for k in have if k[0] == service_id]:
            await have.pop(k).stop()


async def close_all() -> None:
    for have in list(_relays.values()):
        for r in list(have.values()):
            await r.stop()
    _relays.clear()


def status(service_id: int | None = None) -> list[dict]:
    return [r.to_json() for have in _relays.values() for r in have.values()
            if service_id is None or r.service_id == service_id]
