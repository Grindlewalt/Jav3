"""Per-project LAN access: let a project's box reach named devices on the
operator's LAN (a NAS, Home Assistant at 10.0.0.60:8123), box -> LAN only.

OFF by default. When a project turns it on, it lists the targets it may reach:
    10.0.0.0/24          a CIDR inside RFC1918
    10.0.0.60            one address (any port)
    10.0.0.60:8123       one address, one port
    nas.lan / nas.lan:5000   a name the HOST resolver answers with RFC1918 IPs

Enforcement is host-side, in the egress proxy (backend/vm/egress_proxy.py,
_authorize_target): the guest still has no L3 route to the LAN (nftables drops
it); its HTTP(S) traffic reaches the proxy through HTTP(S)_PROXY and the proxy
dials the device from the host. The proxy resolves the target ONCE, judges
every resolved address here, and connects to the address it judged (no second
lookup, so a name cannot rebind between the check and the dial).

Never reachable this way, even when listed or inside a listed CIDR (refused at
save AND at connect): the Jav3 host itself (every address on its interfaces,
its LAN IPs and names, the services' LAN alias), the box networks and their
gateway / proxy address (10.201.0.0/16), loopback, link-local (169.254/16, the
cloud-metadata address), 0.0.0.0/8, multicast and broadcast. Only RFC1918 IPv4
is accepted at all: a public address never goes through this path (the normal
allowlist governs it), and IPv6 is refused.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import time

import aiosqlite

from .config import settings

LAN_PREFIX = "LAN: "
MAX_ENTRIES = 64

RFC1918 = tuple(ipaddress.ip_network(n) for n in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
# Box networks: every tap is 10.201.<cid>.0/30 (host .1 = gateway + proxy).
BOX_NET = ipaddress.ip_network("10.201.0.0/16")
FORBIDDEN_NETS = (
    (ipaddress.ip_network("127.0.0.0/8"), "loopback"),
    (ipaddress.ip_network("0.0.0.0/8"), "an unspecified address"),
    (ipaddress.ip_network("169.254.0.0/16"), "link-local / cloud metadata"),
    (ipaddress.ip_network("224.0.0.0/4"), "multicast"),
    (ipaddress.ip_network("240.0.0.0/4"), "reserved / broadcast"),
    (BOX_NET, "the box network (gateway / proxy)"),
)
FORBIDDEN_NAMES = {"localhost", "metadata", "metadata.google.internal",
                   "instance-data", "instance-data.ec2.internal"}
_NAME_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?"
                      r"(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)*$")
_VIRTUAL_IFACE = re.compile(r"^(docker|br-|veth|virbr|vnet|tap|jvtap|tun|wg|"
                            r"tailscale|zt|podman|cni|flannel|lxc|lxd)")


class LanError(ValueError):
    pass


# --- what "the host" is ------------------------------------------------------

_host_cache: tuple[float, frozenset[str], tuple] | None = None
_HOST_TTL = 60.0


def _scan_host() -> tuple[set[str], list]:
    """(every IPv4 on this machine, the networks of its virtual interfaces)."""
    ips: set[str] = set()
    nets: list = []
    try:
        import ifaddr
        for ad in ifaddr.get_adapters():
            virtual = bool(_VIRTUAL_IFACE.match(ad.nice_name or ""))
            for ip in ad.ips:
                if not isinstance(ip.ip, str):
                    continue
                ips.add(ip.ip)
                if virtual:
                    try:
                        nets.append(ipaddress.ip_network(
                            f"{ip.ip}/{ip.network_prefix}", strict=False))
                    except ValueError:
                        pass
    except Exception:
        pass
    try:
        ips.update(ai[4][0] for ai in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    try:
        from . import lan
        ips.update(lan.lan_ips())
    except Exception:
        pass
    for extra in (settings.vm_egress_host_ip, settings.services_lan_ip):
        if extra:
            ips.add(extra)
    return ips, nets


def host_ips() -> frozenset[str]:
    """Every address that is this Jav3 host. Cached for a minute (DHCP moves)."""
    return _host_view()[0]


def _host_nets() -> tuple:
    return _host_view()[1]


def _host_view():
    global _host_cache
    now = time.monotonic()
    if _host_cache is None or now - _host_cache[0] > _HOST_TTL:
        ips, nets = _scan_host()
        _host_cache = (now, frozenset(ips), tuple(nets))
    return _host_cache[1], _host_cache[2]


def host_names() -> set[str]:
    try:
        from . import lan
        return {n.lower() for n in lan.own_hosts()}
    except Exception:
        return set()


def _resolve4(host: str) -> list[str]:
    """The host resolver's IPv4 answers for a name (blocking). [] = no answer.
    Module-level so tests can replace it."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return list(dict.fromkeys(i[4][0] for i in infos))


# --- judging one address -----------------------------------------------------

def refusal(ip: str) -> str | None:
    """Why this address may never be reached through LAN access, or None."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return f"{ip!r} is not an IP address"
    if a.version == 6:
        if a.ipv4_mapped is None:
            return f"{ip} is IPv6 (LAN access is RFC1918 IPv4 only)"
        a = a.ipv4_mapped
    for net, why in FORBIDDEN_NETS:
        if a in net:
            return f"{a} is {why}"
    if str(a) in host_ips():
        return f"{a} is the Jav3 host itself"
    for net in _host_nets():
        if a in net:
            return f"{a} is on a host-internal network ({net})"
    if not any(a in n for n in RFC1918):
        return f"{a} is not a private LAN (RFC1918) address"
    return None


# --- the allowlist -----------------------------------------------------------

def parse_entry(raw: str, *, resolve: bool = True) -> dict:
    """One allowlist entry -> {kind: cidr|ip|host, value, port, text}.
    Raises LanError naming what is wrong. `resolve` checks a NAME's current
    answers too (at save time); an unresolvable name is kept, and judged by
    its answers at connect time."""
    s = (raw or "").strip().lower().rstrip(".")
    if not s:
        raise LanError("empty entry")
    if "*" in s:
        raise LanError(f"{raw!r}: wildcards are not supported; use a CIDR")
    if s.startswith("[") or s.count(":") > 1:
        raise LanError(f"{raw!r}: IPv6 is not supported (RFC1918 IPv4 only)")
    if "/" in s:
        try:
            net = ipaddress.ip_network(s, strict=False)
        except ValueError:
            raise LanError(f"{raw!r}: not a valid CIDR")
        if net.version != 4 or not any(net.subnet_of(n) for n in RFC1918):
            raise LanError(f"{raw!r}: only RFC1918 ranges (10/8, 172.16/12, "
                           f"192.168/16) may be allowed")
        for fnet, why in FORBIDDEN_NETS:
            if net.subnet_of(fnet):
                raise LanError(f"{raw!r}: {why} is never reachable")
        if net.num_addresses == 1:
            why = refusal(str(net.network_address))
            if why:
                raise LanError(f"{raw!r}: {why}")
        return {"kind": "cidr", "value": str(net), "port": None, "text": str(net)}
    host, port = s, None
    if ":" in s:
        host, _, p = s.partition(":")
        if not p.isdigit() or not (1 <= int(p) <= 65535):
            raise LanError(f"{raw!r}: port must be 1-65535")
        port = int(p)
    if not host:
        raise LanError(f"{raw!r}: missing host")
    text = f"{host}:{port}" if port else host
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        why = refusal(host)
        if why:
            raise LanError(f"{raw!r}: {why}")
        return {"kind": "ip", "value": str(ipaddress.ip_address(host)), "port": port,
                "text": text}
    if not _NAME_RE.match(host):
        raise LanError(f"{raw!r}: not a valid host name")
    if host in FORBIDDEN_NAMES or host.endswith(".localhost"):
        raise LanError(f"{raw!r}: {host} is never reachable")
    if host in host_names():
        raise LanError(f"{raw!r}: {host} is the Jav3 host itself")
    if resolve:
        for ip in _resolve4(host):
            why = refusal(ip)
            if why:
                raise LanError(f"{raw!r}: resolves to {ip} — {why}")
    return {"kind": "host", "value": host, "port": port, "text": text}


def validate(entries) -> list[str]:
    """Normalise a whole list (deduped, order kept). Raises LanError."""
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise LanError("allow must be a list")
    out: list[str] = []
    for e in entries:
        if not isinstance(e, str):
            raise LanError("every entry must be a string")
        if not e.strip():
            continue
        t = parse_entry(e)["text"]
        if t not in out:
            out.append(t)
    if len(out) > MAX_ENTRIES:
        raise LanError(f"at most {MAX_ENTRIES} entries")
    return out


def match(entries: list[str], host: str, port: int | None, ip: str) -> str | None:
    """The entry that lets (host name, port) reach this resolved address."""
    host = (host or "").strip().lower().rstrip(".")
    a = ipaddress.ip_address(ip)
    for raw in entries:
        try:
            e = parse_entry(raw, resolve=False)
        except LanError:
            continue    # a stored entry that is refused now (e.g. the host moved)
        if e["port"] is not None and e["port"] != port:
            continue
        if e["kind"] == "cidr" and a in ipaddress.ip_network(e["value"]):
            return e["text"]
        if e["kind"] == "ip" and e["value"] == str(a):
            return e["text"]
        if e["kind"] == "host" and e["value"] == host:
            return e["text"]
    return None


# --- storage (egress_policy.lan_enabled / lan_allow) -------------------------

async def get(db: aiosqlite.Connection, slug: str | None) -> dict:
    """{enabled, allow}. No row / unattributed / reserved = off."""
    from . import egress
    if egress.is_unattributed(slug) or egress.is_reserved(slug):
        return {"enabled": False, "allow": []}
    async with db.execute("SELECT lan_enabled, lan_allow FROM egress_policy "
                          "WHERE project_slug = ?", (slug,)) as cur:
        r = await cur.fetchone()
    if not r:
        return {"enabled": False, "allow": []}
    try:
        allow = json.loads(r["lan_allow"] or "[]")
    except ValueError:
        allow = []
    return {"enabled": bool(r["lan_enabled"]), "allow": allow}


async def _project_exists(db, slug: str) -> bool:
    async with db.execute("SELECT 1 FROM projects WHERE slug = ? AND deleted_at IS NULL",
                          (slug,)) as cur:
        return await cur.fetchone() is not None


async def set_(db: aiosqlite.Connection, slug: str, *, enabled: bool | None = None,
               allow: list[str] | None = None, actor: str = "operator") -> dict:
    """Update a project's LAN access. Any change raises a `lan_access_changed`
    security event (warn when it is on, info when it was turned off)."""
    from . import egress, security
    if egress.is_unattributed(slug) or egress.is_reserved(slug):
        return {"ok": False, "error": "LAN access is per project"}
    if not await _project_exists(db, slug):
        return {"ok": False, "error": f"unknown project {slug!r}"}
    try:
        allow_n = validate(allow) if allow is not None else None
    except LanError as e:
        return {"ok": False, "error": str(e)}
    cur = await get(db, slug)
    new_enabled = cur["enabled"] if enabled is None else bool(enabled)
    new_allow = cur["allow"] if allow_n is None else allow_n
    async with db.execute("SELECT 1 FROM egress_policy WHERE project_slug = ?",
                          (slug,)) as c:
        exists = await c.fetchone() is not None
    if not exists:
        await db.execute("INSERT INTO egress_policy(project_slug) VALUES (?)", (slug,))
    await db.execute("UPDATE egress_policy SET lan_enabled = ?, lan_allow = ?, "
                     "updated_at = datetime('now') WHERE project_slug = ?",
                     (1 if new_enabled else 0, json.dumps(new_allow), slug))
    await db.commit()
    changed = (new_enabled != cur["enabled"]) or (new_allow != cur["allow"])
    if changed:
        added = [h for h in new_allow if h not in cur["allow"]]
        removed = [h for h in cur["allow"] if h not in new_allow]
        if new_enabled and not cur["enabled"]:
            summary = f"LAN access turned ON for {slug}: {', '.join(new_allow) or '(empty list)'}"
        elif cur["enabled"] and not new_enabled:
            summary = f"LAN access turned off for {slug}"
        else:
            summary = f"LAN allowlist changed for {slug}" + \
                ("" if new_enabled else " (access is off)")
        await security.raise_event(
            db, kind="lan_access_changed",
            severity="warn" if new_enabled else "info", project=slug,
            summary=summary,
            detail={"enabled": new_enabled, "was_enabled": cur["enabled"],
                    "allow": new_allow, "added": added, "removed": removed,
                    "actor": actor})
    return {"ok": True, "slug": slug, "enabled": new_enabled, "allow": new_allow}


# --- the proxy's decision ----------------------------------------------------

def is_lan_target(ips: list[str]) -> bool:
    """A target whose answers include any non-global IPv4 is a LAN target:
    it is judged here, never by the domain allowlist."""
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return True
        if not a.is_global:
            return True
    return False


async def decide(db: aiosqlite.Connection, slug: str | None, host: str,
                 port: int | None, ips: list[str]) -> tuple[str, str, str | None]:
    """(verdict, reason, pinned ip) for a LAN target of a project with LAN
    access ON. Every resolved address must be allowed; the connection is then
    made to the first of them (pinned) so the name is not looked up again.
    Standing refusals still apply first: cut, network off, the deny lists."""
    from . import egress, profiles
    cfg = await get(db, slug)
    if not cfg["enabled"]:
        return "deny", LAN_PREFIX + "access is off for this project", None
    h = egress._norm(host)
    if egress.is_cut(slug, h) or egress.is_cut(egress.GENERAL, h):
        return "cut", "host auto-cut after an anomaly", None
    prof = await profiles.for_slug(db, slug)
    if prof["network_off"]:
        return "deny", f"egress disabled for this project ({prof['name']} profile)", None
    _p_allow, p_deny = await egress.project_lists(db, slug)
    if egress._host_matches(h, p_deny) or egress._host_matches(h, prof["deny_hosts"]):
        return "deny", LAN_PREFIX + "host on a denylist", None
    if not ips:
        return "deny", LAN_PREFIX + f"{h} does not resolve", None
    hits = []
    for ip in ips:
        why = refusal(ip)
        if why:
            return "deny", LAN_PREFIX + why, None
        m = match(cfg["allow"], h, port, ip)
        if not m:
            where = f"{ip}:{port}" if port else ip
            return "deny", LAN_PREFIX + f"{h} ({where}) is not on the project's LAN allowlist", None
        hits.append(m)
    return ("allow", LAN_PREFIX + "on the project's LAN allowlist ("
            + ", ".join(dict.fromkeys(hits)) + ")", ips[0])
