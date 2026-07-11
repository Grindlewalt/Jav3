"""Egress control: the agent asks, the operator grants.

Three ways a sandbox run gets outbound network, all landing in the same
deny-by-default nft allowlist the review console already manages:

  request/approve  the agent's in-VM code hits a wall, files a request naming
                   the host+port it needs and why; the operator approves it into
                   the allowlist (optionally time-limited).
  dev preset       a per-project opt-in that pre-clears the usual dev hosts
                   (PyPI, GitHub, npm, apt…) so pip/git/npm just work.
  YOLO             a global, TTL'd "open the VM's egress" switch — convenient and
                   deliberately blunt: it defeats the exfiltration guard, so it's
                   loud, temporary, and best used on a VM holding nothing sensitive.

Host resolution is best-effort: a CDN name (pypi.org) resolves to rotating IPs,
so a pre-approved IP may not match what the guest later connects to. When that
happens the guest's attempt is simply blocked and shows up as a normal
connection request to approve — no worse than today.
"""
import asyncio
import ipaddress
import re
from pathlib import Path

from . import sandbox
from .config import settings
from .db import get_db

GUEST_IP = settings.vm_ssh_host                 # 10.66.0.10
YOLO_KEY = "yolo_expires_at"
YOLO_COMMENT = "jarvis-yolo"
DNS_LOG = Path("/var/log/jarvis-vm/dns.log")    # dnsmasq guest query/reply log
_REPLY_RE = re.compile(r"reply (\S+) is (\d+\.\d+\.\d+\.\d+)")

# curated dev destinations for the preset (host, port)
DEV_HOSTS = [
    ("pypi.org", 443), ("files.pythonhosted.org", 443),
    ("github.com", 443), ("codeload.github.com", 443),
    ("raw.githubusercontent.com", 443), ("objects.githubusercontent.com", 443),
    ("registry.npmjs.org", 443), ("proxy.golang.org", 443),
    ("crates.io", 443), ("static.crates.io", 443),
    ("deb.debian.org", 443), ("deb.debian.org", 80),
    ("security.debian.org", 443), ("security.debian.org", 80),
]


async def resolve_host(host: str) -> list[str]:
    """Host -> IPv4 addresses (best-effort, same upstream the guest's DNS uses).
    A bare IP resolves to itself."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        infos = await asyncio.get_event_loop().getaddrinfo(
            host, None, family=__import__("socket").AF_INET)
    except OSError:
        return []
    return sorted({i[4][0] for i in infos})


def guest_resolved(host: str, tail_bytes: int = 262144) -> list[str]:
    """The IPs the *guest* actually resolved `host` to, from the dnsmasq log —
    exactly what the guest will connect to (immune to host/guest DNS divergence
    and matching the CDN edge the guest got). Reads only the recent tail."""
    try:
        with open(DNS_LOG, errors="replace") as f:
            size = DNS_LOG.stat().st_size
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            text = f.read()
    except OSError:
        return []
    h = host.lower().rstrip(".")
    out = []
    for m in _REPLY_RE.finditer(text):
        if m.group(1).lower().rstrip(".") == h:
            out.append(m.group(2))
    return list(dict.fromkeys(out))         # unique, resolve-order


async def ips_for(host: str) -> list[str]:
    """Prefer the guest's own resolutions; supplement with a host-side lookup."""
    ips = guest_resolved(host)
    for ip in await resolve_host(host):
        if ip not in ips:
            ips.append(ip)
    return ips


# ---- request / approve -----------------------------------------------------

async def file_request(slug: str, host: str, port: int, proto: str = "tcp",
                       reason: str | None = None) -> dict:
    proto = "udp" if proto == "udp" else "tcp"
    db = await get_db()
    try:
        # collapse duplicate pending asks for the same dest
        async with db.execute(
            "SELECT id FROM egress_requests WHERE project_slug=? AND host=? AND "
            "port=? AND proto=? AND status='pending'", (slug, host, int(port), proto)) as cur:
            existing = await cur.fetchone()
        if existing:
            rid = existing["id"]
        else:
            cur = await db.execute(
                "INSERT INTO egress_requests (project_slug, host, port, proto, reason) "
                "VALUES (?, ?, ?, ?, ?)", (slug, host, int(port), proto, reason))
            rid = cur.lastrowid
            await db.commit()
        async with db.execute("SELECT * FROM egress_requests WHERE id=?", (rid,)) as cur:
            return dict(await cur.fetchone())
    finally:
        await db.close()


async def list_requests(status: str | None = "pending",
                        slug: str | None = None) -> list[dict]:
    q = "SELECT * FROM egress_requests"
    conds, args = [], []
    if status:
        conds.append("status = ?"); args.append(status)
    if slug:
        conds.append("project_slug = ?"); args.append(slug)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT 100"
    db = await get_db()
    try:
        async with db.execute(q, tuple(args)) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _decide(rid: int, status: str) -> dict:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE egress_requests SET status=?, decided_at=datetime('now') WHERE id=?",
            (status, rid))
        await db.commit()
        async with db.execute("SELECT * FROM egress_requests WHERE id=?", (rid,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    if row is None:
        raise KeyError(f"no egress request #{rid}")
    return dict(row)


async def approve_request(rid: int, ttl_minutes: int | None = None) -> dict:
    """Resolve the requested host and add allowlist rules for every IP it maps
    to (best-effort), then mark the request approved."""
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM egress_requests WHERE id=?", (rid,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    if row is None:
        raise KeyError(f"no egress request #{rid}")
    req = dict(row)
    ips = await ips_for(req["host"])
    for ip in ips:
        await sandbox.add_rule(dest=req["host"], ip=ip, port=req["port"],
                               proto=req["proto"], scope="wan",
                               note=f"egress req #{rid} ({req['project_slug']})",
                               ttl_minutes=ttl_minutes)
    out = await _decide(rid, "approved")
    out["allowed_ips"] = ips
    return out


async def deny_request(rid: int) -> dict:
    return await _decide(rid, "denied")


# ---- dev preset ------------------------------------------------------------

async def apply_dev_preset(slug: str, ttl_minutes: int | None = None) -> dict:
    """Resolve + allowlist the curated dev hosts for a project."""
    added = []
    for host, port in DEV_HOSTS:
        for ip in await ips_for(host):
            await sandbox.add_rule(dest=host, ip=ip, port=port, proto="tcp",
                                   scope="wan", note=f"dev preset ({slug})",
                                   ttl_minutes=ttl_minutes)
            added.append({"host": host, "ip": ip, "port": port})
    return {"added": len(added), "hosts": sorted({h for h, _ in DEV_HOSTS})}


async def refresh_domains() -> int:
    """Keep allowlisted *hostnames* current as the guest re-resolves them (CDN
    rotation): for each rule whose dest is a domain, add any freshly guest-
    resolved IP not already allowlisted (short TTL; the sweep prunes the old).
    This is what makes 'allow by DNS name' hold up over time."""
    rules = await sandbox.list_rules(include_expired=False)
    have = {(r["ip"], r["port"], r["proto"]) for r in rules}
    domains: dict[tuple, None] = {}
    for r in rules:
        try:
            ipaddress.ip_address(r["dest"])          # dest is a bare IP -> skip
            continue
        except ValueError:
            pass
        domains[(r["dest"], r["port"], r["proto"])] = None
    added = 0
    for host, port, proto in domains:
        for ip in guest_resolved(host):
            if (ip, port, proto) not in have:
                await sandbox.add_rule(dest=host, ip=ip, port=port, proto=proto,
                                       scope="wan", note=f"dns-refresh {host}",
                                       ttl_minutes=180)
                have.add((ip, port, proto))
                added += 1
    return added


async def set_project_mode(slug: str, mode: str | None) -> None:
    mode = mode if mode in ("dev", "locked") else None
    db = await get_db()
    try:
        await db.execute("UPDATE projects SET egress_mode=? WHERE slug=?",
                         (None if mode in (None, "locked") else mode, slug))
        await db.commit()
    finally:
        await db.close()


# ---- YOLO (global, TTL'd, blunt) -------------------------------------------

async def _yolo_handle() -> int | None:
    """Handle of the live YOLO accept rule in guest_forward, or None."""
    code, out, _ = await sandbox._nft(
        "-a", "list", "chain", "inet", settings.nft_table, "guest_forward")
    if code != 0:
        return None
    for line in out.splitlines():
        # match the rule by our comment OR its distinctive content, so deletion
        # works regardless of how nft echoes the comment back
        if YOLO_COMMENT in line or f"saddr {GUEST_IP} accept" in line:
            m = re.search(r"# handle (\d+)", line)
            if m:
                return int(m.group(1))
    return None


async def yolo_on(ttl_minutes: int = 60) -> dict:
    """Open the guest's egress entirely (insert an accept rule above the drop).
    TTL'd so it auto-closes; the sweep enforces expiry."""
    if await _yolo_handle() is None:
        await sandbox._nft("insert", "rule", "inet", settings.nft_table,
                           "guest_forward", "ip", "saddr", GUEST_IP, "accept",
                           "comment", f'"{YOLO_COMMENT}"')
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO session_state (key, value) "
            "VALUES (?, datetime('now', ?))", (YOLO_KEY, f"+{int(ttl_minutes)} minutes"))
        await db.commit()
    finally:
        await db.close()
    return await yolo_status()


async def yolo_off() -> dict:
    handle = await _yolo_handle()
    if handle is not None:
        await sandbox._nft("delete", "rule", "inet", settings.nft_table,
                           "guest_forward", "handle", str(handle))
    db = await get_db()
    try:
        await db.execute("DELETE FROM session_state WHERE key=?", (YOLO_KEY,))
        await db.commit()
    finally:
        await db.close()
    return {"on": False, "expires_at": None}


async def yolo_status() -> dict:
    db = await get_db()
    try:
        async with db.execute("SELECT value FROM session_state WHERE key=?",
                              (YOLO_KEY,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    on = await _yolo_handle() is not None
    return {"on": on, "expires_at": row["value"] if row else None}


async def sweep_yolo() -> bool:
    """Close YOLO if its TTL has lapsed. Returns True if it closed something."""
    db = await get_db()
    try:
        async with db.execute(
            "SELECT value FROM session_state WHERE key=? AND value <= datetime('now')",
            (YOLO_KEY,)) as cur:
            expired = await cur.fetchone()
    finally:
        await db.close()
    if expired or (await _yolo_handle() is not None and not await _has_yolo_state()):
        await yolo_off()
        return True
    return False


async def _has_yolo_state() -> bool:
    db = await get_db()
    try:
        async with db.execute("SELECT 1 FROM session_state WHERE key=?", (YOLO_KEY,)) as cur:
            return await cur.fetchone() is not None
    finally:
        await db.close()
