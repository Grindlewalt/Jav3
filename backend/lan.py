"""LAN presence: mDNS advertisement, the server's own LAN identity, and a
reachability probe for the companion services.

The server is LAN-first: this module is what lets a user open
`http://<name>.local:<port>` instead of typing an IP. Everything here is
best-effort — a box with no multicast (or no zeroconf installed) still boots,
it just isn't discoverable. Nothing here scans the network: identity comes from
the local interfaces, and the probe only dials the configured service URLs.
"""
import asyncio
import ipaddress
import logging
import re
import socket
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends

from .auth import require_user
from .config import settings

log = logging.getLogger("jav3.lan")

SERVICE_TYPE = "_http._tcp.local."
# Interfaces that are never "the LAN": container bridges, VM taps/bridges.
_VIRTUAL_IFACE = re.compile(r"^(lo|docker|br-|veth|virbr|vnet|tap|jvtap|tun|wg|tailscale|zt)")

_state: dict = {"name": "", "hostname": "", "advertised": False, "error": ""}
_own: list[str] | None = None   # own_hosts(), derived once (at start) and cached
_zc = None          # AsyncZeroconf while registered
_info = None        # the registered ServiceInfo


def _label(raw: str) -> str:
    """Squash anything into one DNS label (a-z0-9-, <=63)."""
    s = re.sub(r"[^a-z0-9-]+", "-", (raw or "").lower()).strip("-")
    return s[:63].strip("-")


def instance_name(app_title: str = "") -> str:
    """Configured instance_name, else the app's own name (its first word,
    lowercased), else the machine's hostname."""
    if settings.instance_name.strip():
        name = _label(settings.instance_name)
    else:
        name = _label((app_title or "").split()[0] if (app_title or "").split() else "")
    return name or _label(socket.gethostname().split(".")[0]) or "server"


def lan_ips() -> list[str]:
    """Private, non-loopback IPv4 addresses on this box's real interfaces."""
    ips: list[str] = []
    try:
        import ifaddr   # zeroconf dependency
        for ad in ifaddr.get_adapters():
            if _VIRTUAL_IFACE.match(ad.nice_name or ""):
                continue
            ips += [ip.ip for ip in ad.ips if isinstance(ip.ip, str)]
    except Exception:
        try:
            ips = [ai[4][0] for ai in socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET)]
        except OSError:
            ips = []
    out = []
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.is_private and not a.is_loopback and not a.is_link_local \
                and ip != settings.vm_egress_host_ip and ip not in out:
            out.append(ip)
    return out


def own_hosts() -> list[str]:
    """Every name this server answers to on the LAN: the mDNS name, the machine
    hostname (bare and .local) and its LAN IPs. Lowercased, deduped. Derived
    once — the CSRF check reads this on every state-changing request."""
    global _own
    if _own is None:
        host = socket.gethostname().split(".")[0].lower()
        names = [_state["hostname"] or f"{instance_name()}.local", host,
                 f"{host}.local" if host else "", *lan_ips()]
        _own = list(dict.fromkeys(n.lower() for n in names if n))
    return _own


def advertised_hostname() -> str:
    """The mDNS name (`<name>.local`) while the advertisement is live, else ''."""
    return _state["hostname"] if _state["advertised"] else ""


def media_hosts() -> list[str]:
    return list(dict.fromkeys([*(h.lower() for h in settings.media_hosts),
                               *own_hosts()]))


# --- mDNS ---------------------------------------------------------------------

async def start(app_title: str = "") -> None:
    """Advertise over mDNS. Never raises: a failure is a logged warning."""
    global _zc, _info, _own
    name = instance_name(app_title)
    _state.update(name=name, hostname=f"{name}.local", advertised=False, error="")
    _own = None
    own_hosts()
    if not settings.mdns:
        _state["error"] = "disabled (JARVIS_MDNS=false)"
        return
    try:
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf
        ips = lan_ips()
        if not ips:
            raise OSError("no LAN IPv4 address to advertise")
        _info = ServiceInfo(
            SERVICE_TYPE, f"{name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip) for ip in ips],
            port=settings.lan_port, properties={"path": "/"},
            server=f"{name}.local.")
        _zc = AsyncZeroconf()
        # the first await queues the registration, the second is its probing
        # + announce actually finishing (zeroconf's two-step async API)
        pending = await _zc.async_register_service(_info, allow_name_change=True)
        await asyncio.wait_for(pending, 10)
        _state["advertised"] = True
        log.info("mDNS: advertising http://%s.local:%s", name, settings.lan_port)
    except Exception as e:  # noqa: BLE001 — discovery is optional
        _state["error"] = f"{type(e).__name__}: {e}"
        log.warning("mDNS advertisement unavailable (%s); reach the server by IP",
                    _state["error"])
        await stop()


async def stop() -> None:
    global _zc, _info
    zc, info, _zc, _info = _zc, _info, None, None
    _state["advertised"] = False
    if zc is None:
        return
    try:
        if info is not None:
            await asyncio.wait_for(await zc.async_unregister_service(info), 5)
        await asyncio.wait_for(zc.async_close(), 5)
    except Exception as e:  # noqa: BLE001
        log.warning("mDNS unregister failed: %s", e)


# --- companion-service probe ----------------------------------------------------

def services() -> dict[str, str]:
    return {"searxng": settings.searxng_url,
            "voice_sidecar": settings.voice_sidecar_url,
            "voice_local": settings.voice_local_base_url}


async def _reachable(url: str, timeout: float = 1.0) -> bool:
    p = urlsplit(url)
    port = p.port or (443 if p.scheme in ("https", "wss") else 80)
    if not p.hostname:
        return False
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(p.hostname, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    w.close()
    return True


async def probe() -> list[dict]:
    """One ~1s TCP connect per configured service, all at once."""
    items = list(services().items())
    ok = await asyncio.gather(*(_reachable(u) for _, u in items))
    return [{"service": k, "url": u, "reachable": r} for (k, u), r in zip(items, ok)]


async def status() -> dict:
    return {"name": _state["name"], "hostname": _state["hostname"],
            "advertised": _state["advertised"], "mdns_error": _state["error"],
            "ips": lan_ips(), "port": settings.lan_port,
            "services_host": settings.services_host,
            "services": await probe()}


router = APIRouter(prefix="/api/lan", dependencies=[Depends(require_user)])


@router.get("")
async def get_lan():
    """What the server advertised and where the LAN can reach it."""
    return await status()
