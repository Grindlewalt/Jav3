"""Sandbox review console — the deterministic layer over gate captures.

Two jobs:

1. The persistent egress allowlist (`sandbox_rules`). Approving a destination
   in the console writes a row AND programs the live nftables set; revoking
   removes both; `sync_nft()` re-loads every rule into the (empty-on-boot) set
   at backend startup, so learned rules survive a reboot regardless of unit
   ordering.

2. `classify()` turns a run's raw capture (gate.py's evidence.json) into the
   verdict + per-connection findings the console renders. **Everything here is
   rule-based arithmetic over typed fields** — no LLM, and untrusted strings
   (hostnames, argv, paths) are only ever compared or counted, never
   interpreted. That is deliberate: the console is the thing that catches a
   compromised agent, so it must not itself be steerable by the agent's output.

Deny-by-default means a non-allowlisted connection is dropped at the tap: the
handshake never completes, so its intended payload size is unknowable. Blocked
findings therefore carry an *attempt count* (from the nft drop log) and never a
byte figure; only allowlisted, delivered flows report bytes (from the pcap).
"""
import asyncio
import ipaddress
import re

from .config import settings
from .db import get_db


# ---- nftables allowlist ----------------------------------------------------

async def _nft(*args: str, timeout: float = 15) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "nft", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except (FileNotFoundError, OSError):     # no sudo/nft (dev host) — no-op
        return -1, "", "nft unavailable"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "timeout"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _set_for(proto: str) -> str:
    return "allowed_udp" if proto == "udp" else "allowed_tcp"


async def _nft_add(ip: str, port: int, proto: str) -> None:
    await _nft("add", "element", "inet", settings.nft_table, _set_for(proto),
               f"{{ {ip} . {port} }}")


async def _nft_del(ip: str, port: int, proto: str) -> None:
    await _nft("delete", "element", "inet", settings.nft_table, _set_for(proto),
               f"{{ {ip} . {port} }}")


async def sync_nft() -> int:
    """Re-program every persisted rule into the live nft sets. Best-effort:
    on a host without the table (dev laptop) this quietly does nothing."""
    rules = await list_rules()
    n = 0
    for r in rules:
        try:
            ipaddress.ip_address(r["ip"])
        except ValueError:
            continue
        await _nft_add(r["ip"], r["port"], r["proto"])
        n += 1
    return n


async def list_rules() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, dest, ip, port, proto, scope, note, created_at "
            "FROM sandbox_rules ORDER BY created_at DESC, id DESC")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def add_rule(dest: str, ip: str, port: int, proto: str = "tcp",
                   scope: str = "wan", note: str | None = None) -> dict:
    ipaddress.ip_address(ip)          # reject anything that isn't an address
    port = int(port)
    proto = "udp" if proto == "udp" else "tcp"
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO sandbox_rules (dest, ip, port, proto, scope, note) "
            "VALUES (?, ?, ?, ?, ?, ?)", (dest, ip, port, proto, scope, note))
        await db.commit()
        cur = await db.execute(
            "SELECT id, dest, ip, port, proto, scope, note, created_at "
            "FROM sandbox_rules WHERE ip=? AND port=? AND proto=?", (ip, port, proto))
        row = dict(await cur.fetchone())
    finally:
        await db.close()
    await _nft_add(ip, port, proto)   # live-allow; safe to re-add
    return row


async def delete_rule(rule_id: int) -> bool:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT ip, port, proto FROM sandbox_rules WHERE id=?", (rule_id,))
        row = await cur.fetchone()
        if not row:
            return False
        await db.execute("DELETE FROM sandbox_rules WHERE id=?", (rule_id,))
        await db.commit()
    finally:
        await db.close()
    await _nft_del(row["ip"], row["port"], row["proto"])
    return True


async def rules_index() -> dict[tuple, dict]:
    """(ip, port, proto) -> rule, for O(1) allowlist membership in classify()."""
    return {(r["ip"], r["port"], r["proto"]): r for r in await list_rules()}


# ---- classification (pure, given the rule index) ---------------------------

def is_lan(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in settings.lan_cidrs:
        if addr in ipaddress.ip_network(cidr):
            return True
    return False


def _glob_to_re(pattern: str) -> re.Pattern:
    # minimal ** / * / ? glob -> regex; ** spans path separators, * does not.
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern[i:i + 2] == "**":
            out.append(".*")
            i += 2
            if pattern[i:i + 1] == "/":
                i += 1
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


_SENSITIVE_RES = None


def _sensitive_globs():
    global _SENSITIVE_RES
    if _SENSITIVE_RES is None:
        _SENSITIVE_RES = [(g, _glob_to_re(g)) for g in settings.sandbox_sensitive_globs]
    return _SENSITIVE_RES


def match_sensitive(path: str) -> str | None:
    p = path.strip()
    if p.startswith("./"):
        p = p[2:]
    for glob, rx in _sensitive_globs():
        if rx.match(p) or rx.match(p.split("/")[-1]):
            return glob
    return None


# path-ish tokens inside an argv line (skip flags, urls handled separately)
_PATHISH = re.compile(r"(?:^|\s)((?:/|\./|\.\./|[\w.-]+/)[\w./-]+|\.[\w.-]+)")
_URL_HOST = re.compile(r"(?:https?://|ssh://|git@)([\w.-]+)")


def _hosts_in_exec(cmd: str) -> list[str]:
    return _URL_HOST.findall(cmd)


# ---- behavioral rules (deterministic, injection-safe) ----------------------
# Detonation heuristics over the streams we already capture (auditd argv, the
# net drop/flow tables, DNS, staged writes). Each rule is a compiled pattern or
# an arithmetic threshold; matched strings are only compared/counted and echoed
# back as data (escaped by the JSON layer), never interpreted. These catch the
# classic malware behaviours the operator listed: download-and-exec, reverse
# shells, persistence, LAN scanning, C2 beaconing.

_INTERP = r"(?:ba|z|k|da|a)?sh|python[0-9.]*|perl|ruby|node|php|lua"

# a fetch tool whose output is piped straight into an interpreter, or fetched to
# a file that is then made executable — the download-and-run pattern.
_DOWNLOAD_EXEC = [
    re.compile(r"\b(?:curl|wget|fetch)\b[^\n|]*\|\s*(?:" + _INTERP + r")\b", re.I),
    re.compile(r"\b(?:curl|wget)\b[^\n]*-o\s*\S+[^\n]*&&[^\n]*\bchmod\b[^\n]*\+x", re.I),
    re.compile(r"\bbase64\b[^\n|]*-d[^\n|]*\|\s*(?:" + _INTERP + r")\b", re.I),
]

# interactive shell handed to a remote socket.
_REVERSE_SHELL = [
    re.compile(r"/dev/(?:tcp|udp)/", re.I),                       # bash builtin
    re.compile(r"\bn(?:c|cat|etcat)\b[^\n]*\s-[a-z]*e\b", re.I),  # nc -e / ncat -e
    re.compile(r"\bsocat\b[^\n]*\bexec:", re.I),
    re.compile(r"\bmkfifo\b[^\n]*(?:\bnc\b|\|\s*(?:" + _INTERP + r")\b)", re.I),
    re.compile(r"\bpython[0-9.]*\b[^\n]*\bsocket\b[^\n]*(?:subprocess|/bin/sh|dup2|pty)", re.I),
]

# writes/edits to a location that survives a reboot / re-run.
_PERSISTENCE_GLOBS = [
    "**/.ssh/authorized_keys", "**/authorized_keys",
    "**/.bashrc", "**/.bash_profile", "**/.profile", "**/.zshrc",
    "/etc/cron*/**", "/etc/cron*", "/var/spool/cron/**",
    "/etc/systemd/**", "**/.config/systemd/user/**", "/lib/systemd/system/**",
    "/etc/rc.local", "/etc/ld.so.preload", "/etc/profile.d/**",
]
_PERSIST_EXEC = re.compile(
    r"\b(?:crontab\s+-|systemctl\s+enable|update-rc\.d|"
    r"echo[^\n]*>>[^\n]*(?:authorized_keys|\.bashrc|\.profile|rc\.local))", re.I)

# thresholds for the fan-out / repetition rules (kept generous to avoid noise)
_LANSCAN_HOSTS = 8          # distinct LAN dests in one run == sweep
_LANSCAN_PORTS = 12         # distinct ports on a single host == port scan
_BEACON_ATTEMPTS = 10       # repeated hits to one dropped dest == beaconing


_PERSIST_RES = None


def _persist_globs():
    global _PERSIST_RES
    if _PERSIST_RES is None:
        _PERSIST_RES = [(g, _glob_to_re(g)) for g in _PERSISTENCE_GLOBS]
    return _PERSIST_RES


def match_persistence(path: str) -> str | None:
    p = path.strip()
    if p.startswith("./"):
        p = p[2:]
    for glob, rx in _persist_globs():
        if rx.match(p) or rx.match(p.split("/")[-1]):
            return glob
    return None


def behavior_findings(evidence: dict) -> list[dict]:
    """Rule hits over the captured streams. Pure: evidence dict -> findings.
    Each finding = {kind, sev, rule, evidence}. `evidence` is untrusted data
    (an argv line, a path, or a count) shown as text, never interpreted."""
    out: list[dict] = []

    def add(kind, sev, rule, ev):
        out.append({"kind": kind, "sev": sev, "rule": rule, "evidence": ev})

    execs = evidence.get("execs", [])
    for cmd in execs:
        if any(rx.search(cmd) for rx in _DOWNLOAD_EXEC):
            add("download-exec", "crit", "fetch piped to an interpreter", cmd)
        if any(rx.search(cmd) for rx in _REVERSE_SHELL):
            add("reverse-shell", "crit", "interactive shell to a socket", cmd)
        if _PERSIST_EXEC.search(cmd):
            add("persistence", "crit", "persistence-establishing command", cmd)

    # persistence via staged writes / argv path tokens
    persist_paths = set()
    for p in evidence.get("staged", []):
        g = match_persistence(p)
        if g and p not in persist_paths:
            persist_paths.add(p)
            add("persistence", "crit", f"writes {g}", p)
    for cmd in execs:
        for tok in _PATHISH.findall(cmd):
            g = match_persistence(tok)
            if g and tok not in persist_paths:
                persist_paths.add(tok)
                add("persistence", "crit", f"touches {g}", tok)

    # LAN sweep / port scan over the drop + flow tables
    net = list(evidence.get("blocked", [])) + list(evidence.get("flows", []))
    lan_hosts, ports_per_host = set(), {}
    for n in net:
        ip = n.get("ip", "")
        if is_lan(ip):
            lan_hosts.add(ip)
            ports_per_host.setdefault(ip, set()).add(n.get("port"))
    if len(lan_hosts) >= _LANSCAN_HOSTS:
        sev = "crit" if len(lan_hosts) >= _LANSCAN_HOSTS * 2 else "warn"
        add("lan-scan", sev, f"reached {len(lan_hosts)} LAN hosts", str(len(lan_hosts)))
    for ip, ports in ports_per_host.items():
        if len(ports) >= _LANSCAN_PORTS:
            add("port-scan", "warn", f"{len(ports)} ports on one host", ip)

    # C2 beaconing: many repeated attempts to a single dropped dest
    for b in evidence.get("blocked", []):
        att = int(b.get("attempts", 0))
        if att >= _BEACON_ATTEMPTS and not is_lan(b.get("ip", "")):
            host = b.get("host") or b.get("ip", "")
            add("beaconing", "warn", f"{att} repeated attempts to one host", host)

    return out


def classify(evidence: dict, rule_index: dict[tuple, dict],
             blocklist=None) -> dict:
    """Raw capture + current allowlist -> the console's session view.

    `blocklist` is an optional threatintel.Blocklist; a matched destination is a
    hard critical that no allowlist approval can clear. None => no reputation
    matching (keeps classify pure and testable without feeds on disk)."""
    allow_hosts = {r["dest"] for r in rule_index.values()}

    def _ti(ip="", host=""):
        return bool(blocklist) and blocklist.hit(ip, host)

    egress: list[dict] = []
    delivered_bytes = 0

    # delivered flows (handshake completed => these were allowlisted)
    for f in evidence.get("flows", []):
        ip, port, proto = f["ip"], int(f["port"]), f.get("proto", "tcp")
        host = f.get("host") or ip
        down, up = int(f.get("bytes_down", 0)), int(f.get("bytes_up", 0))
        delivered_bytes += down + up
        egress.append({
            "key": f"{host}:{port}", "host": host, "ip": ip, "port": port,
            "proto": proto, "scope": "lan" if is_lan(ip) else "wan",
            "learned": True, "status": "delivered", "sev": "ok",
            "rule": "in-allowlist",
            "bytes": _human(down + up), "dir": "down" if down >= up else "up",
            "attempts": 0,
        })

    # blocked attempts (dropped at the tap => attempt count, never bytes)
    blocked_attempts = 0
    for b in evidence.get("blocked", []):
        ip, port, proto = b["ip"], int(b["port"]), b.get("proto", "tcp")
        host = b.get("host") or ip
        attempts = int(b.get("attempts", 1))
        blocked_attempts += attempts
        lan = is_lan(ip)
        learned = (ip, port, proto) in rule_index or host in allow_hosts
        if learned:
            sev, status, rule = "ok", "delivered", "in-allowlist"
        elif lan:
            sev, status, rule = "warn", "blocked", "lan-not-allowlisted"
        else:
            sev, status, rule = "warn", "blocked", "egress-not-allowlisted"
        egress.append({
            "key": f"{host}:{port}", "host": host, "ip": ip, "port": port,
            "proto": proto, "scope": "lan" if lan else "wan",
            "learned": learned, "status": status, "sev": sev, "rule": rule,
            "bytes": None, "dir": "up", "attempts": attempts,
        })

    # execs: flag any that reach for a non-allowlisted host (curl/git push/etc.)
    execs = []
    exec_crit = False
    for cmd in evidence.get("execs", []):
        sev, rule = "ok", None
        for host in _hosts_in_exec(cmd):
            if host not in allow_hosts and not is_lan_host(host):
                sev, rule = "crit", f"reaches {host} (not allowlisted)"
                exec_crit = True
                break
        execs.append({"cmd": cmd, "sev": sev, "rule": rule})

    # sensitive: capture-supplied hits, plus argv path tokens matched to globs
    sensitive = []
    seen_paths = set()
    for s in evidence.get("sensitive", []):
        p = s["path"]
        if p not in seen_paths:
            seen_paths.add(p)
            sensitive.append({"path": p, "glob": s.get("glob", ""), "sev": "crit"})
    for cmd in evidence.get("execs", []):
        for tok in _PATHISH.findall(cmd):
            g = match_sensitive(tok)
            if g and tok not in seen_paths:
                seen_paths.add(tok)
                sensitive.append({"path": tok, "glob": g, "sev": "crit"})
    for p in evidence.get("staged", []):
        g = match_sensitive(p)
        if g and p not in seen_paths:
            seen_paths.add(p)
            sensitive.append({"path": p, "glob": g, "sev": "warn"})

    dns = [{"name": d.get("name", ""), "type": d.get("type", ""),
            "new": d.get("name", "") not in allow_hosts}
           for d in evidence.get("dns", [])]
    staged = [{"path": p} for p in evidence.get("staged", [])]

    # threat-intel reputation: a captured dest on a known-bad feed is a hard
    # critical and non-clearable. Overrides the allowlist — even an approved dest
    # that later shows up on a feed flags. Marks the row so the console/API can
    # refuse to allowlist it.
    threat = []
    for e in egress:
        if _ti(e["ip"], e["host"]):
            e["sev"], e["blocklisted"] = "crit", True
            e["rule"] = "threat-intel: known-bad host"
            threat.append({"kind": "egress", "host": e["host"], "ip": e["ip"],
                           "port": e["port"]})
    for d in dns:
        if _ti(host=d["name"]):
            d["threat"] = True
            threat.append({"kind": "dns", "host": d["name"]})

    # beacon-catcher: a rendered artifact's network attempts (payload visible)
    beacons, artifact = [], None
    render = evidence.get("render")
    if render:
        artifact = render.get("artifact")
        for a in render.get("attempts", []):
            host = _url_host(a.get("url", ""))
            external = bool(host) and not is_lan_host(host) and host not in allow_hosts
            beacons.append({
                "api": a.get("api", ""), "method": a.get("method", ""),
                "url": a.get("url", ""), "host": host or "",
                "bytes": _human(a.get("bytes", 0)) if a.get("bytes") else "",
                "external": external, "sev": "crit" if external else "ok",
            })
    beacon_ext = sum(1 for b in beacons if b["external"])

    egress_new = sum(1 for e in egress if not e["learned"])
    lan_hosts = sum(1 for e in egress if e["scope"] == "lan")

    # behavioral detonation rules over the same captured streams
    behavior = behavior_findings(evidence)
    behavior_crit = next((b for b in behavior if b["sev"] == "crit"), None)
    behavior_warn = next((b for b in behavior if b["sev"] == "warn"), None)

    # offline scanner hits (ClamAV signatures, YARA patterns) over the run's
    # output files — a signature match is a hard critical.
    scan = evidence.get("scan") or {}
    scan_hits = ([{"engine": "clamav", "path": h.get("path", ""),
                   "signature": h.get("signature", "")} for h in scan.get("clamav", [])]
                 + [{"engine": "yara", "path": h.get("path", ""),
                     "signature": h.get("rule", "")} for h in scan.get("yara", [])])
    # capa is informational only (lists what a binary can do; never a verdict driver)
    capa = [c for c in scan.get("capa", []) if c.get("capabilities")]

    # Suricata network signatures over the captured pcap (typed by the parser)
    suricata = list(evidence.get("suricata") or [])
    suricata_crit = any(s.get("sev") == "crit" for s in suricata)

    # deterministic verdict = worst signal present
    crit_sens = any(s["sev"] == "crit" for s in sensitive)
    if threat:
        verdict, rule = "crit", "threat-intel:known-bad-destination"
    elif scan_hits:
        verdict, rule = "crit", f"malware-signature:{scan_hits[0]['engine']}"
    elif suricata_crit:
        verdict, rule = "crit", "suricata:network-signature"
    elif beacon_ext:
        verdict, rule = "crit", "dashboard-beacon"
    elif behavior_crit:
        verdict, rule = "crit", f"behavior:{behavior_crit['kind']}"
    elif crit_sens or exec_crit:
        verdict, rule = "crit", ("sensitive-path-read" if crit_sens
                                 else "exec-reaches-untrusted-host")
    elif behavior_warn:
        verdict, rule = "warn", f"behavior:{behavior_warn['kind']}"
    elif suricata:
        verdict, rule = "warn", "suricata:network-signature"
    elif egress_new:
        verdict, rule = "warn", "new-destination-blocked"
    else:
        verdict, rule = "ok", "nothing-outside-allowlist"

    facts = {
        "dns": len(dns), "egress_dests": len(egress), "egress_new": egress_new,
        "blocked_attempts": blocked_attempts, "delivered_bytes": _human(delivered_bytes),
        "sensitive": len(sensitive), "execs": len(execs), "staged": len(staged),
        "lan_hosts": lan_hosts, "beacons": len(beacons), "beacons_external": beacon_ext,
        "behavior": len(behavior), "threat": len(threat), "scan": len(scan_hits),
        "suricata": len(suricata),
    }
    return {
        "verdict": verdict, "rule": rule, "headline": _headline(facts, verdict),
        "facts": facts, "egress": egress, "dns": dns,
        "sensitive": sensitive, "execs": execs, "staged": staged,
        "beacons": beacons, "artifact": artifact, "behavior": behavior,
        "threat": threat, "scan": scan_hits, "scan_ran": scan.get("ran", []),
        "suricata": suricata, "capa": capa,
    }


def _url_host(url: str) -> str:
    m = re.match(r"^(?:https?:)?//([^/:?#]+)", url.strip(), re.I)
    return m.group(1).lower() if m else ""


def is_lan_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".local") or host == "localhost"
    return is_lan(host)


def _headline(f: dict, verdict: str) -> str:
    p = []
    if f.get("scan"):
        n = f["scan"]
        p.append(f"{n} malware signature hit{'s' if n > 1 else ''}")
    if f.get("suricata"):
        n = f["suricata"]
        p.append(f"{n} network signature{'s' if n > 1 else ''} (Suricata)")
    if f.get("threat"):
        n = f["threat"]
        p.append(f"{n} known-bad destination{'s' if n > 1 else ''} (threat feed)")
    if f.get("beacons_external"):
        n = f["beacons_external"]
        p.append(f"artifact beaconed to {n} external host{'s' if n > 1 else ''}")
    if f.get("behavior"):
        n = f["behavior"]
        p.append(f"{n} behavioral flag{'s' if n > 1 else ''}")
    if f["egress_new"]:
        p.append(f"{f['egress_new']} new destination{'s' if f['egress_new'] > 1 else ''} blocked")
    if f["sensitive"]:
        p.append(f"{f['sensitive']} sensitive path{'s' if f['sensitive'] > 1 else ''} touched")
    if f["lan_hosts"]:
        p.append(f"{f['lan_hosts']} LAN host{'s' if f['lan_hosts'] > 1 else ''}")
    if not p:
        p.append("nothing outside the allowlist")
    return " · ".join(p)


def _human(n: int) -> str:
    n = int(n)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
