"""Egress anomaly detection — the exfil-shaped-behaviour half of Layer 3.

Run by the proxy ONLY on requests it actually allowed (a denied host never went
anywhere, so there is nothing to watch). Three detectors, matching the operator's
picks — new/unapproved hosts deliberately do NOT trip these:

  • high-entropy host   — a random-looking domain name; the DGA tell.
  • volume spike        — bytes to one host far above this project's baseline.
  • beacon cadence      — near-perfectly regular connections to one host (C2).

A trip returns an anomaly dict; the proxy cuts the host (egress.mark_cut + an
nftables drop) and raises a security_event. Once cut, egress.decide short-
circuits, so a detector fires at most once per host.

Volume and cadence look at `egress_anomaly_window_seconds` before this host's
latest allowed hit, never all history. Summed over all time, any host a
project keeps using eventually looks like a spike (a daily 25 KB pip session
to files.pythonhosted.org crossed 1 MB after 40 days and was cut, Pi
2026-09-07), and a daily schedule's requests are a perfectly regular
86400 s "beacon". Anchoring on the latest hit rather than the wall clock keeps
the judgement the same whenever it runs.

Entropy is judged on the REGISTRABLE domain (eTLD+1: `gvt1.com`), not on the
host. A CDN names its nodes at random (`r11---sn-bvvbaxivnuxqjvhj5nu-nx5k.
gvt1.com`, entropy 4.10 against 3.8) and cut Google's Chromium downloads twice
on the Pi (2026-10-01): the agent's browser broke, both cuts were false. What a
DGA randomises is the name it registered, so that is what is measured. A host
on the project's (or its profile's) allowlist is not judged on entropy at all:
someone already decided it. Volume and cadence still apply to every host.
"""
import math
from datetime import datetime

import aiosqlite

from .config import settings

# how far back each history-based detector looks
_WINDOW = 200


def entropy_bits_per_char(s: str) -> float:
    """Shannon entropy of the hostname's characters (dots stripped)."""
    chars = [c for c in s.lower() if c != "."]
    if not chars:
        return 0.0
    n = len(chars)
    freq: dict[str, int] = {}
    for c in chars:
        freq[c] = freq.get(c, 0) + 1
    return -sum((k / n) * math.log2(k / n) for k in freq.values())


# second-level labels that sit under a country code as a public suffix
# (example.co.uk, example.com.au); the short list of the ones in real use
_CC_SECOND = frozenset({"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "go",
                        "gob", "mil", "sch", "nom"})


def registrable_domain(host: str) -> str:
    """The domain a name was registered as (eTLD+1): `r4---sn-x.gvt1.com` ->
    `gvt1.com`, `a.b.example.co.uk` -> `example.co.uk`. An IP literal or a
    one-label name is returned as it is. A heuristic, not the public suffix list:
    it is only used to decide WHAT to measure."""
    h = (host or "").strip().lower().rstrip(".")
    labels = h.split(".")
    if len(labels) <= 2 or ":" in h or all(p.isdigit() for p in labels):
        return h
    if len(labels[-1]) == 2 and labels[-2] in _CC_SECOND and len(labels) > 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


async def _allowlisted(db: aiosqlite.Connection, slug: str | None, host: str) -> bool:
    """Is the host on the project's or its profile's allowlist (an explicit
    decision: not allow-by-default, not an auto-allow)? Unsure reads as no."""
    try:
        from . import egress
        pol = await egress.get_policy(db, slug)
        return egress._host_matches(host, pol["effective_allow"])
    except Exception:  # noqa: BLE001
        return False


def _parse(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return None


async def _window_start(db: aiosqlite.Connection, slug: str | None, host: str) -> str | None:
    """The start of the judged span: the window before this host's latest
    allowed hit (None = the host has no allowed hits)."""
    async with db.execute(
            # datetime() inside: an unparseable stamp reads NULL and is skipped
            # rather than becoming the anchor (and nulling the whole window)
            "SELECT datetime(MAX(datetime(created_at)), ?) AS s FROM egress_events "
            "WHERE verdict='allow' AND host = ? AND (project_slug IS ? OR ? IS NULL)",
            (f"-{int(settings.egress_anomaly_window_seconds)} seconds",
             host, slug, slug)) as cur:
        r = await cur.fetchone()
    return r["s"] if r else None


async def _host_volume(db: aiosqlite.Connection, slug: str | None, host: str,
                       since: str) -> tuple[int, list[int]]:
    """(this host's bytes_out in the window, per-host totals in the same
    window for the project's OTHER hosts)."""
    async with db.execute(
            "SELECT host, SUM(bytes_out) AS b FROM egress_events "
            "WHERE verdict='allow' AND (project_slug IS ? OR ? IS NULL) "
            "AND created_at >= ? GROUP BY host", (slug, slug, since)) as cur:
        rows = await cur.fetchall()
    this_total, others = 0, []
    for r in rows:
        if r["host"] == host:
            this_total = r["b"] or 0
        else:
            others.append(r["b"] or 0)
    return this_total, others


async def _host_gaps(db: aiosqlite.Connection, slug: str | None, host: str,
                     since: str) -> list[float]:
    async with db.execute(
            "SELECT created_at FROM egress_events WHERE verdict='allow' AND host = ? "
            "AND (project_slug IS ? OR ? IS NULL) AND created_at >= ? "
            "ORDER BY id DESC LIMIT ?",
            (host, slug, slug, since, _WINDOW)) as cur:
        times = [_parse(r["created_at"]) for r in await cur.fetchall()]
    times = [t for t in times if t is not None]
    times.reverse()
    return [b - a for a, b in zip(times, times[1:])]


async def check_host(db: aiosqlite.Connection, slug: str | None, host: str) -> dict | None:
    """Return the first anomaly for this host, or None. Called after an allowed
    request is recorded (so the just-seen event is in the history)."""
    domain = registrable_domain(host)
    ent = entropy_bits_per_char(domain)
    if ent >= settings.egress_entropy_threshold and not await _allowlisted(db, slug, host):
        return {"kind": "high_entropy",
                "summary": f"high-entropy host {host} (entropy {ent:.2f})",
                "detail": {"host": host, "domain": domain, "entropy": round(ent, 2),
                           "threshold": settings.egress_entropy_threshold}}

    since = await _window_start(db, slug, host)
    if since is None:
        return None
    this_total, others = await _host_volume(db, slug, host, since)
    if this_total >= settings.egress_volume_min_bytes:
        baseline = (sorted(others)[len(others) // 2] if others else 0)
        if this_total > settings.egress_volume_multiple * max(baseline, 1):
            return {"kind": "volume_spike",
                    "summary": f"volume spike to {host} ({this_total} bytes)",
                    "detail": {"host": host, "bytes_out": this_total,
                               "baseline": baseline,
                               "multiple": settings.egress_volume_multiple}}

    gaps = await _host_gaps(db, slug, host, since)
    if len(gaps) >= settings.egress_beacon_min_hits - 1:
        mean = sum(gaps) / len(gaps)
        if mean > 0:
            var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
            cv = math.sqrt(var) / mean
            if cv <= settings.egress_beacon_cv_max:
                return {"kind": "beacon_cadence",
                        "summary": f"beacon-like cadence to {host} (~{mean:.0f}s, cv {cv:.2f})",
                        "detail": {"host": host, "period_seconds": round(mean, 1),
                                   "cv": round(cv, 3), "hits": len(gaps) + 1}}
    return None
