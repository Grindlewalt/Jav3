"""Threat-intel blocklist — deterministic reputation matching over public
known-bad feeds, refreshed *offline* on the host.

The agent never triggers a fetch and never sees feed contents; the classifier
only compares a run's captured destinations (IPs, hostnames, DNS names) against
the loaded sets. This preserves the sandbox threat model: a listed destination
is a hard **critical** and is **never auto-clearable** — the review console
refuses to add an allowlist rule for it (contrast the "unknown host → orange,
clears on approval" path, which stays as-is).

Feeds live under `data/threatintel/`:
  ips.txt      one IPv4 address or CIDR per line  (abuse.ch Feodo, Spamhaus DROP)
  domains.txt  one domain per line                (abuse.ch URLhaus host list)
  meta.json    {sources: [...], fetched_at, counts}

Everything degrades to "no matches" when the files are absent (dev laptop, or
before the first refresh), so the classifier is unaffected until feeds land.
"""
import ipaddress
import json
from pathlib import Path

from .config import settings

# Public, well-known offline feeds. Refreshed host-side (never by the agent).
FEEDS = [
    ("feodo", "https://feodotracker.abuse.ch/downloads/ipblocklist.txt", "ip"),
    ("spamhaus-drop", "https://www.spamhaus.org/drop/drop.txt", "cidr"),
    ("urlhaus", "https://urlhaus.abuse.ch/downloads/hostfile/", "domain"),
]


def _dir() -> Path:
    return settings.data_dir / "threatintel"


class Blocklist:
    """Loaded feed sets + O(1)/O(cidrs) membership. Immutable after build."""

    def __init__(self, ips=None, cidrs=None, domains=None, meta=None):
        self.ips = ips or set()
        self.cidrs = cidrs or []              # list[ip_network]
        self.domains = domains or set()
        self.meta = meta or {}

    def __bool__(self):
        return bool(self.ips or self.cidrs or self.domains)

    def match_ip(self, ip: str) -> bool:
        if not ip:
            return False
        if ip in self.ips:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.cidrs)

    def match_host(self, host: str) -> bool:
        """Exact domain or any parent domain (sub.evil.com matches evil.com)."""
        if not host:
            return False
        h = host.strip().lower().rstrip(".")
        # a bare IP host goes through match_ip, not the domain set
        try:
            ipaddress.ip_address(h)
            return self.match_ip(h)
        except ValueError:
            pass
        parts = h.split(".")
        for i in range(len(parts) - 1):
            if ".".join(parts[i:]) in self.domains:
                return True
        return False

    def hit(self, ip: str = "", host: str = "") -> bool:
        return self.match_ip(ip) or self.match_host(host)


def _parse_lines(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.split(";", 1)[0].split("#", 1)[0].strip()
        if line:
            out.append(line.split()[0])       # drop trailing feed annotations
    return out


def _parse_domain_lines(text: str) -> list[str]:
    """Domain feeds come plain (one host per line) or in /etc/hosts format
    (`0.0.0.0 baddomain`). Take the domain token, never the redirect IP."""
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        toks = line.split()
        cand = toks[-1] if len(toks) >= 2 else toks[0]   # hostfile -> last tok
        try:
            ipaddress.ip_address(cand)                    # a bare IP is not a domain
            continue
        except ValueError:
            pass
        if "." in cand and "/" not in cand:
            out.append(cand.lower().rstrip("."))
    return out


def build_from(ip_text: str, domain_text: str, meta: dict | None = None) -> Blocklist:
    """Pure builder — used by the loader and directly by tests."""
    ips, cidrs = set(), []
    for tok in _parse_lines(ip_text):
        if "/" in tok:
            try:
                cidrs.append(ipaddress.ip_network(tok, strict=False))
            except ValueError:
                continue
        else:
            try:
                ipaddress.ip_address(tok)
                ips.add(tok)
            except ValueError:
                continue
    domains = {d.lower().rstrip(".") for d in _parse_lines(domain_text)
               if "." in d and "/" not in d}
    return Blocklist(ips, cidrs, domains, meta or {})


_CACHE: tuple[float, Blocklist] | None = None


def load() -> Blocklist:
    """mtime-cached load of the on-disk feeds. Empty Blocklist when absent."""
    global _CACHE
    d = _dir()
    ip_f, dom_f, meta_f = d / "ips.txt", d / "domains.txt", d / "meta.json"
    try:
        mtime = max((f.stat().st_mtime for f in (ip_f, dom_f) if f.exists()),
                    default=0.0)
    except OSError:
        mtime = 0.0
    if mtime == 0.0:
        return Blocklist()
    if _CACHE and _CACHE[0] == mtime:
        return _CACHE[1]
    ip_text = ip_f.read_text(errors="replace") if ip_f.exists() else ""
    dom_text = dom_f.read_text(errors="replace") if dom_f.exists() else ""
    meta = {}
    if meta_f.exists():
        try:
            meta = json.loads(meta_f.read_text())
        except (OSError, ValueError):
            meta = {}
    bl = build_from(ip_text, dom_text, meta)
    _CACHE = (mtime, bl)
    return bl


async def refresh(timeout: float = 30) -> dict:
    """Download the feeds host-side and write the files. Best-effort per feed;
    a feed that fails to fetch leaves the previous copy in place. Returns a
    summary. This is operator/host infra — never reachable from an agent tool."""
    import datetime as _dt

    import httpx

    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    ip_lines, dom_lines, sources = [], [], []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        for name, url, kind in FEEDS:
            try:
                r = await c.get(url)
                r.raise_for_status()
                toks = _parse_lines(r.text)
            except Exception as e:                       # noqa: BLE001 (best-effort)
                sources.append({"name": name, "url": url, "error": str(e)})
                continue
            if kind == "domain":
                dom_lines += _parse_domain_lines(r.text)
            else:
                ip_lines += toks
            sources.append({"name": name, "url": url, "count": len(toks)})
    (d / "ips.txt").write_text("\n".join(sorted(set(ip_lines))) + "\n")
    (d / "domains.txt").write_text("\n".join(sorted(set(dom_lines))) + "\n")
    meta = {"sources": sources,
            "fetched_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "ips": len(set(ip_lines)), "domains": len(set(dom_lines))}
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    global _CACHE
    _CACHE = None                                        # force reload next call
    return meta
