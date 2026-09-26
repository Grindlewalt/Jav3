"""The Persistent view's data: which non-OS processes run in each box, what
they are connected to, and whether the host agrees (WP4, DESIGN-BOXES 2(b)).

Source. Every `vm_procwatch_seconds` the host asks each running box for a
guest/backend/procwatch.py snapshot: `{"mode":"ps"}` on the run-turn port
(shared/project boxes) or on svcd's port (service boxes). The reply is
UNTRUSTED: read with a byte cap and a timeout, then re-validated field by field
(`sanitize_snapshot`). A guest can lie about itself; it cannot crash, stall or
flood the host.

"Not OS". A process is OS if it is a kernel thread, the guest's own server
(self_pid), or its (exe, systemd unit) is in the image's baseline.json
(recorded by the image build, WP5; a built-in minimal set when none exists).
Everything else is shown, with all of its descendants under it. Tags:
  service     cgroup unit jav3-svc-<id>.service, <id> approved for this box
  run_code    turn boxes: in the guest server's unit (what run_code spawned,
              nohup'd or not) but not the server itself
  unexpected  anything else, and raises `unexpected_process` (warn; critical
              in a service box), once per (box boot, exe, unit)

Host truth. Once per cycle the host samples ITS OWN kernel (`ss -tinH`) for
TCP sockets whose peer is a box's guest IP: the proxy's accepted sockets and
the service relay's outbound ones. Those are joined to the guest's socket by
the reversed 4-tuple, which gives host-verified byte counts that the guest
cannot touch. `host` names come from the proxy's egress_events rows joined on
(box_id, peer_port) (WP2's columns) or a live-connection source it registers.
`proc_report_mismatch` fires when, on two consecutive cycles, the host holds an
established connection from the guest that no reported process owns, or the
guest's byte counters for a connection disagree with the host's by more than
5% (plus slack). Inbound service sessions (service_port_events, WP3's relay)
are summed onto the listening socket.
"""
from __future__ import annotations

import asyncio
import fnmatch
import importlib.util
import json
import logging
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import settings
from . import boxes

log = logging.getLogger(__name__)

PROCS_CHAN = "procs"                # bus channel; the /api/events topic `procs`

# --- host-side caps on what a guest may send -------------------------------
MAX_REPLY_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT_S = 3.0
MAX_PROCS = 2048
MAX_SOCKS = 4096
MAX_INODES_PER_PROC = 256
MAX_CONNS_PER_PROC = 64
MAX_TREE_DEPTH = 64
MAX_EVENT_BYTES = 256 * 1024        # a bigger box row is announced, not pushed
MAX_ALERTS_PER_BOX_HOUR = 10
_MAX_INT = 2 ** 53
_MAX_PID = 4194304                  # PID_MAX_LIMIT
_STR = {"exe": 256, "cmd": 512, "comm": 64, "user": 32, "unit": 128,
        "cgroup": 256, "state": 12, "laddr": 64, "raddr": 64, "proto": 5,
        "boot_id": 64, "error": 200}
_PROTOS = ("tcp", "tcp6", "udp", "udp6")

BYTE_TOLERANCE = 0.05
BYTE_SLACK = 64 * 1024
MISS_CYCLES = 2                     # consecutive cycles before a mismatch alerts

SVC_UNIT = re.compile(r"^jav3-svc-(\d{1,9})\.service$")
# short-lived helpers svcd spawns inside its own unit (systemd-run, ...)
SVCD_HELPERS = ("/usr/bin/systemd-run", "/usr/bin/systemctl", "/usr/bin/journalctl",
                "/bin/systemd-run", "/bin/systemctl", "/bin/journalctl")
# used only when the image recorded no baseline.json
BUILTIN_BASELINE: tuple[tuple[str, str], ...] = (
    ("*", "init.scope"),
    ("*", "systemd-*.service"),
    ("/usr/lib/systemd/*", "*"),
    ("/lib/systemd/*", "*"),
    ("*", "dbus.service"),
    ("*", "cron.service"),
    ("/usr/sbin/agetty", "*getty@*.service"),
    ("/sbin/agetty", "*getty@*.service"),
    ("*", "cloud-*.service"),
    ("*", "rsyslog.service"),
)


# --- sanitising ---------------------------------------------------------------

_CTRL = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029\\u202a-\\u202e\\u2066-\\u2069]")


def clean_str(v: Any, key: str) -> str:
    """Guest text -> bounded, printable text (bidi/controls replaced)."""
    if v is None:
        return ""
    if not isinstance(v, str):
        v = str(v) if isinstance(v, (int, float)) else ""
    v = v[:_STR.get(key, 256) * 2]
    return _CTRL.sub("?", v)[:_STR.get(key, 256)]


def clean_int(v: Any, lo: int = 0, hi: int = _MAX_INT) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        if isinstance(v, float) and v == v and lo <= v <= hi:
            return int(v)
        return None
    return v if lo <= v <= hi else None


def sanitize_snapshot(raw: Any) -> dict:
    """A guest's snapshot, re-validated: bad entries dropped, strings cleaned
    and clipped, numbers bounded, lists capped. Raises ValueError only when
    the whole thing is unusable."""
    if not isinstance(raw, dict):
        raise ValueError("snapshot is not an object")
    procs_in = raw.get("procs")
    socks_in = raw.get("socks")
    if not isinstance(procs_in, list) or not isinstance(socks_in, list):
        raise ValueError("snapshot lacks procs/socks")
    truncated = bool(raw.get("truncated") is True)
    if len(procs_in) > MAX_PROCS or len(socks_in) > MAX_SOCKS:
        truncated = True
    procs: dict[int, dict] = {}
    for p in procs_in[:MAX_PROCS]:
        if not isinstance(p, dict):
            continue
        pid = clean_int(p.get("pid"), 1, _MAX_PID)
        if pid is None or pid in procs:
            continue
        ppid = clean_int(p.get("ppid"), 0, _MAX_PID)
        inodes = p.get("inodes") if isinstance(p.get("inodes"), list) else []
        procs[pid] = {
            "pid": pid, "ppid": 0 if ppid is None or ppid == pid else ppid,
            "uid": clean_int(p.get("uid"), 0, 2 ** 32),
            "user": clean_str(p.get("user"), "user"),
            "comm": clean_str(p.get("comm"), "comm"),
            "exe": clean_str(p.get("exe"), "exe"),
            "cmd": clean_str(p.get("cmd"), "cmd"),
            "cgroup": clean_str(p.get("cgroup"), "cgroup"),
            "unit": clean_str(p.get("unit"), "unit"),
            "kthread": p.get("kthread") is True,
            "state": clean_str(p.get("state"), "state"),
            "rss": clean_int(p.get("rss")),
            "cpu_ticks": clean_int(p.get("cpu_ticks")),
            "start_ticks": clean_int(p.get("start_ticks")),
            "inodes": [i for i in (clean_int(x, 1, 2 ** 64) for x in inodes[:MAX_INODES_PER_PROC])
                       if i is not None],
        }
    socks: list[dict] = []
    for s in socks_in[:MAX_SOCKS]:
        if not isinstance(s, dict):
            continue
        proto = s.get("proto")
        if proto not in _PROTOS:
            continue
        socks.append({
            "proto": proto,
            "laddr": clean_str(s.get("laddr"), "laddr"),
            "lport": clean_int(s.get("lport"), 0, 65535),
            "raddr": clean_str(s.get("raddr"), "raddr"),
            "rport": clean_int(s.get("rport"), 0, 65535),
            "state": clean_str(s.get("state"), "state"),
            "inode": clean_int(s.get("inode"), 0, 2 ** 64) or 0,
            "bytes_sent": clean_int(s.get("bytes_sent")),
            "bytes_received": clean_int(s.get("bytes_received")),
        })
    uptime = raw.get("uptime_s")
    return {"boot_id": clean_str(raw.get("boot_id"), "boot_id"),
            "self_pid": clean_int(raw.get("self_pid"), 1, _MAX_PID),
            "uptime_s": float(uptime) if isinstance(uptime, (int, float))
            and not isinstance(uptime, bool) and 0 <= uptime < 1e10 else None,
            "clk_tck": clean_int(raw.get("clk_tck"), 1, 100000) or 100,
            "procs": procs, "socks": socks, "truncated": truncated}


# --- baseline -------------------------------------------------------------------

@dataclass
class Baseline:
    exact: frozenset = frozenset()
    patterns: tuple = ()
    source: str = "builtin"          # "image" | "builtin"

    def matches(self, exe: str, unit: str) -> bool:
        if (exe, unit) in self.exact:
            return True
        return any(fnmatch.fnmatchcase(exe, e) and fnmatch.fnmatchcase(unit, u)
                   for e, u in self.patterns)


# Units whose processes are never "OS" by virtue of the image baseline: the
# guest server's own unit (what it spawns is run_code / svcd's business) and
# approved-service units (tagged by cgroup, never baselined).
_NEVER_BASELINE_UNITS = re.compile(r"^(jarvis-guest\.service|jav3-svc-.*)$")
_UNIT_NAME = re.compile(r"^[A-Za-z0-9@._:\\-]{1,200}\.(service|scope)$")


def entries_from_image_baseline(data: dict) -> list[dict]:
    """WP5's image baseline (`<image>.baseline.json`: dpkg, pip, npm,
    units_enabled, setuid, listening, processes) -> WP4 entries.

    Every enabled .service unit of the quiescent image becomes ("*", unit):
    any process in that unit's cgroup is OS. `processes` are `ps comm` names
    (truncated, no path, no unit) and are NOT turned into entries: a bare name
    would whitelist that name in every cgroup (the capture itself runs
    python3/ps/sh), so it would hide exactly the implant this view exists to
    show. The built-in systemd/getty/dbus patterns are kept alongside."""
    out = [{"exe": e, "unit": u} for e, u in BUILTIN_BASELINE]
    seen = set()
    for u in data.get("units_enabled") or []:
        if (isinstance(u, str) and _UNIT_NAME.match(u)
                and not _NEVER_BASELINE_UNITS.match(u) and u not in seen):
            seen.add(u)
            out.append({"exe": "*", "unit": u})
    return out


def parse_baseline(data: Any, source: str = "image") -> Baseline:
    """baseline.json: {"v":1, "entries":[{"exe","unit"}, ...]} or a bare list
    of {"exe","unit"} / [exe, unit]. Entries with * or ? are glob patterns.
    WP5's image baseline shape (units_enabled, processes, ...) is converted
    by entries_from_image_baseline."""
    if (isinstance(data, dict) and "entries" not in data
            and ("units_enabled" in data or "processes" in data)):
        data = {"v": 1, "entries": entries_from_image_baseline(data)}
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError("baseline has no entries")
    exact, pats = set(), []
    for e in entries[:10000]:
        if isinstance(e, dict):
            exe, unit = e.get("exe"), e.get("unit")
        elif isinstance(e, (list, tuple)) and len(e) == 2:
            exe, unit = e
        else:
            continue
        if not isinstance(exe, str) or not isinstance(unit, (str, type(None))):
            continue
        unit = unit or ""
        if any(c in exe + unit for c in "*?["):
            pats.append((exe, unit))
        else:
            exact.add((exe, unit))
    return Baseline(frozenset(exact), tuple(pats), source)


BUILTIN = Baseline(frozenset(), BUILTIN_BASELINE, "builtin")
_baseline_resolvers: list[Callable[[Any], Path | None]] = []
_baseline_cache: dict[str, tuple[float, Baseline]] = {}


def add_baseline_resolver(fn: Callable[[Any], Path | None]) -> None:
    """WP5 may register fn(box) -> path of that box image's baseline.json."""
    if fn not in _baseline_resolvers:
        _baseline_resolvers.append(fn)


async def _baseline_path_from_db(db, box) -> Path | None:
    variant, version = box.image
    try:
        if version is None:
            q = ("SELECT baseline_path FROM image_versions WHERE variant=? AND active=1 "
                 "AND baseline_path IS NOT NULL ORDER BY version DESC LIMIT 1")
            args: tuple = (variant,)
        else:
            q = ("SELECT baseline_path FROM image_versions WHERE variant=? AND version=? "
                 "AND baseline_path IS NOT NULL")
            args = (variant, int(str(version).lstrip("v")))
        async with db.execute(q, args) as cur:
            r = await cur.fetchone()
    except Exception:  # noqa: BLE001 — no table / bad version: fall through
        return None
    return Path(r[0]) if r and r[0] else None


async def baseline_for(db, box) -> Baseline:
    path = None
    for fn in _baseline_resolvers:
        try:
            path = fn(box)
        except Exception:  # noqa: BLE001
            path = None
        if path:
            break
    if path is None and db is not None:
        path = await _baseline_path_from_db(db, box)
    if path is None and box.image[0] == "main":
        cand = Path(settings.vm_dir) / "baseline.json"
        path = cand if cand.exists() else None
    if path is None:
        return BUILTIN
    try:
        mtime = path.stat().st_mtime
        hit = _baseline_cache.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("baseline too large")
        b = parse_baseline(json.loads(path.read_text()))
        _baseline_cache[str(path)] = (mtime, b)
        return b
    except (OSError, ValueError) as e:
        log.warning("procview: baseline %s unusable (%s); using built-in", path, e)
        return BUILTIN


# --- classification + tree ------------------------------------------------------

def _children_map(procs: dict[int, dict]) -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for pid, p in procs.items():
        if p["ppid"] in procs and p["ppid"] != pid:
            kids.setdefault(p["ppid"], []).append(pid)
    for v in kids.values():
        v.sort()
    return kids


def _descendants(root: int, kids: dict[int, list[int]], limit: int = MAX_PROCS) -> set[int]:
    seen: set[int] = set()
    stack = [root]
    while stack and len(seen) < limit:
        pid = stack.pop()
        for c in kids.get(pid, ()):
            if c not in seen and c != root:
                seen.add(c)
                stack.append(c)
    return seen


def classify(snap: dict, box, baseline: Baseline,
             approved: set[int] | None) -> dict[int, str]:
    """pid -> tag for every process that is NOT OS (before descendants)."""
    procs = snap["procs"]
    self_pid = snap["self_pid"] if snap["self_pid"] in procs else None
    self_unit = procs[self_pid]["unit"] if self_pid else ""
    kids = _children_map(procs)
    self_desc = _descendants(self_pid, kids) if self_pid else set()
    turn_box = box.kind in ("shared", "project")
    tags: dict[int, str] = {}
    for pid, p in procs.items():
        if p["kthread"] or pid == self_pid:
            continue
        m = SVC_UNIT.match(p["unit"])
        if m:
            sid = int(m.group(1))
            ok = approved is None or sid in approved
            tags[pid] = "service" if ok else "unexpected"
            continue
        if turn_box and self_pid and (pid in self_desc or
                                      (self_unit and p["unit"] == self_unit)):
            tags[pid] = "run_code"
            continue
        if (not turn_box and pid in self_desc and p["exe"] in SVCD_HELPERS):
            continue
        if baseline.matches(p["exe"], p["unit"]):
            continue
        tags[pid] = "unexpected"
    return tags


def shown_tags(snap: dict, tags: dict[int, str]) -> dict[int, str]:
    """Every non-OS process plus all its descendants; a descendant without a
    tag of its own takes its nearest tagged ancestor's."""
    procs = snap["procs"]
    kids = _children_map(procs)
    out = dict(tags)
    for pid in sorted(tags):
        for d in _descendants(pid, kids):
            out.setdefault(d, tags[pid])
    return out


# --- connections + host join ------------------------------------------------------

def _tuple(s: dict) -> tuple:
    return (s["laddr"], s["lport"], s["raddr"], s["rport"])


def _within(a: int, b: int) -> bool:
    return abs(a - b) <= max(BYTE_SLACK, BYTE_TOLERANCE * max(a, b))


def build_conns(snap: dict, box, host_socks: dict[tuple, dict],
                hostnames: dict[int, str], inbound: dict[int, dict]
                ) -> tuple[dict[int, list[dict]], list[dict], set[tuple]]:
    """(pid -> conn rows, orphan conn rows, guest tuples owned by a process).
    `host_socks` is keyed by the GUEST's view of the tuple (already reversed);
    `hostnames` maps a guest local port to the proxy's host for it;
    `inbound` maps a listening port to the relay's open-session byte totals."""
    owner: dict[int, int] = {}
    for pid in sorted(snap["procs"]):
        for ino in snap["procs"][pid]["inodes"]:
            owner.setdefault(ino, pid)
    listening = {(s["proto"][:3], s["lport"]) for s in snap["socks"]
                 if s["state"] in ("LISTEN", "UNCONN")}
    by_pid: dict[int, list[dict]] = {}
    orphans: list[dict] = []
    owned: set[tuple] = set()
    for s in snap["socks"]:
        pid = owner.get(s["inode"]) if s["inode"] else None
        if s["state"] == "TIME-WAIT" or (pid is None and not s["inode"]):
            continue                              # kernel-held, nobody's
        is_in = s["state"] in ("LISTEN", "UNCONN") or \
            (s["proto"][:3], s["lport"]) in listening
        row = {"proto": s["proto"], "dir": "in" if is_in else "out",
               "laddr": s["laddr"], "lport": s["lport"],
               "raddr": s["raddr"] or None, "rport": s["rport"] or None,
               "host": None, "state": s["state"],
               "guest_bytes_out": s["bytes_sent"], "guest_bytes_in": s["bytes_received"],
               "host_bytes_out": None, "host_bytes_in": None, "verified": None}
        if s["state"] in ("LISTEN", "UNCONN"):
            row["raddr"] = row["rport"] = None
        hs = host_socks.get(_tuple(s)) if s["proto"].startswith("tcp") else None
        if hs is not None:
            row["host_bytes_out"] = hs.get("bytes_received")
            row["host_bytes_in"] = hs.get("bytes_sent")
            row["verified"] = _verify(row)
        if not is_in and s["raddr"] == box.host_ip:
            row["host"] = hostnames.get(s["lport"])
        if s["state"] == "LISTEN" and s["lport"] in inbound:
            agg = inbound[s["lport"]]
            row["host_bytes_in"] = agg.get("bytes_in")
            row["host_bytes_out"] = agg.get("bytes_out")
        if pid is None:
            orphans.append(row)
            continue
        owned.add(_tuple(s))
        lst = by_pid.setdefault(pid, [])
        if len(lst) < MAX_CONNS_PER_PROC:
            lst.append(row)
    return by_pid, orphans, owned


def _verify(row: dict) -> bool | None:
    pairs = [(row["guest_bytes_out"], row["host_bytes_out"]),
             (row["guest_bytes_in"], row["host_bytes_in"])]
    pairs = [(g, h) for g, h in pairs if g is not None and h is not None]
    if not pairs:
        return None
    return all(_within(g, h) for g, h in pairs)


# --- per-box state across cycles ---------------------------------------------------

@dataclass
class BoxState:
    box_id: str
    snap: dict | None = None
    reported_at: float | None = None
    error: str | None = None
    boot_id: str = ""
    baseline_source: str = "builtin"
    prev_cpu: dict = field(default_factory=dict)     # pid -> (start, ticks, uptime)
    miss: dict = field(default_factory=dict)         # key -> consecutive cycles
    alerted: set = field(default_factory=set)
    alert_times: deque = field(default_factory=lambda: deque(maxlen=MAX_ALERTS_PER_BOX_HOUR))
    row: dict | None = None


_state: dict[str, BoxState] = {}


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cpu_pct(st: BoxState, snap: dict, pid: int) -> float | None:
    p = snap["procs"][pid]
    up = snap["uptime_s"]
    prev = st.prev_cpu.get(pid)
    if prev is None or up is None or p["cpu_ticks"] is None:
        return None
    start, ticks, pup = prev
    if start != p["start_ticks"] or up <= pup or p["cpu_ticks"] < ticks:
        return None
    pct = 100.0 * (p["cpu_ticks"] - ticks) / snap["clk_tck"] / (up - pup)
    return round(min(pct, 100.0 * 1024), 1)


def _started(snap: dict, pid: int, now: float) -> str | None:
    p = snap["procs"][pid]
    if snap["uptime_s"] is None or p["start_ticks"] is None:
        return None
    age = snap["uptime_s"] - p["start_ticks"] / snap["clk_tck"]
    if not 0 <= age < 1e9:
        return None
    return _iso(now - age)


def render_tree(snap: dict, tags: dict[int, str], conns: dict[int, list[dict]],
                box, st: BoxState, now: float) -> list[dict]:
    """Nested rows for the shown pids. Cycle-safe (a hostile ppid graph can
    loop) and depth-capped."""
    procs = snap["procs"]
    shown = set(tags)
    kids: dict[int, list[int]] = {}
    for pid in shown:
        pp = procs[pid]["ppid"]
        if pp in shown and pp != pid:
            kids.setdefault(pp, []).append(pid)
    placed: set[int] = set()

    def node(pid: int, depth: int) -> dict:
        placed.add(pid)
        p = procs[pid]
        m = SVC_UNIT.match(p["unit"])
        n = {"pid": pid, "ppid": p["ppid"], "user": p["user"],
             "exe": p["exe"], "cmd": p["cmd"] or p["comm"], "unit": p["unit"] or None,
             "service_id": int(m.group(1)) if m else None, "tag": tags[pid],
             "rss": p["rss"], "cpu_pct": _cpu_pct(st, snap, pid),
             "started": _started(snap, pid, now),
             "conns": conns.get(pid, []), "children": []}
        if depth < MAX_TREE_DEPTH:
            for c in sorted(kids.get(pid, ())):
                if c not in placed:
                    n["children"].append(node(c, depth + 1))
        return n

    roots = sorted(pid for pid in shown if procs[pid]["ppid"] not in shown)
    tree = [node(pid, 0) for pid in roots]
    # whatever is left sits on a ppid cycle: each cycle becomes its own root
    for pid in sorted(shown):
        if pid not in placed:
            tree.append(node(pid, 0))
    return tree


# --- one box, one cycle (pure given its inputs) -------------------------------------

def evaluate(box, snap: dict, st: BoxState, *, baseline: Baseline,
             approved: set[int] | None, host_socks: dict[tuple, dict] | None,
             hostnames: dict[int, str], inbound: dict[int, dict],
             now: float) -> tuple[dict, list[dict]]:
    """Build the API row for one box and the alerts it warrants. Updates the
    box's cross-cycle state (cpu baselines, miss streaks, dedupe)."""
    if snap["boot_id"] != st.boot_id:
        st.boot_id = snap["boot_id"]
        st.prev_cpu.clear()
        st.miss.clear()
        st.alerted.clear()
    st.baseline_source = baseline.source
    own = classify(snap, box, baseline, approved)
    tags = shown_tags(snap, own)
    by_pid, orphans, owned = build_conns(snap, box, host_socks or {}, hostnames, inbound)
    tree = render_tree(snap, tags, by_pid, box, st, now)
    alerts: list[dict] = []

    # unexpected processes, by their OWN classification (a baseline child
    # shown under an unexpected parent is not a second alert)
    for pid, tag in sorted(own.items()):
        if tag != "unexpected":
            continue
        p = snap["procs"][pid]
        key = ("unexpected", p["exe"] or p["comm"], p["unit"])
        if key in st.alerted:
            continue
        st.alerted.add(key)
        alerts.append({
            "kind": "unexpected_process",
            "severity": "critical" if box.kind == "service" else "warn",
            "summary": f"Unexpected process in box {box.id}: "
                       f"{(p['exe'] or p['comm'] or '?')[:120]}",
            "detail": {"box_id": box.id, "pid": pid, "exe": p["exe"], "cmd": p["cmd"],
                       "unit": p["unit"], "user": p["user"],
                       "baseline": baseline.source}})

    # host-held connections from this guest that no reported process owns
    if host_socks is not None:
        seen_keys = set()
        for t, hs in host_socks.items():
            if hs.get("state") != "ESTAB":
                continue
            if t in owned:
                continue
            k = ("unreported",) + t
            seen_keys.add(k)
            st.miss[k] = st.miss.get(k, 0) + 1
            if st.miss[k] >= MISS_CYCLES and k not in st.alerted:
                st.alerted.add(k)
                alerts.append({
                    "kind": "proc_report_mismatch", "severity": "warn",
                    "summary": f"Box {box.id}: host sees a connection from guest port "
                               f"{t[1]} that no reported process owns",
                    "detail": {"box_id": box.id, "reason": "unreported_connection",
                               "guest": f"{t[0]}:{t[1]}", "peer": f"{t[2]}:{t[3]}",
                               "host": hostnames.get(t[1]),
                               "host_bytes_out": hs.get("bytes_received"),
                               "host_bytes_in": hs.get("bytes_sent")}})
        # byte disagreements on connections both sides report
        for pid, rows in by_pid.items():
            for r in rows:
                if r["verified"] is not False:
                    continue
                t = (r["laddr"], r["lport"], r["raddr"], r["rport"])
                k = ("bytes",) + t
                seen_keys.add(k)
                st.miss[k] = st.miss.get(k, 0) + 1
                if st.miss[k] >= MISS_CYCLES and k not in st.alerted:
                    st.alerted.add(k)
                    alerts.append({
                        "kind": "proc_report_mismatch", "severity": "warn",
                        "summary": f"Box {box.id}: pid {pid} reports different byte "
                                   f"counts than the host saw on port {r['lport']}",
                        "detail": {"box_id": box.id, "reason": "byte_mismatch", "pid": pid,
                                   "exe": snap["procs"][pid]["exe"],
                                   "guest": f"{t[0]}:{t[1]}", "peer": f"{t[2]}:{t[3]}",
                                   "guest_bytes_out": r["guest_bytes_out"],
                                   "host_bytes_out": r["host_bytes_out"],
                                   "guest_bytes_in": r["guest_bytes_in"],
                                   "host_bytes_in": r["host_bytes_in"]}})
        for k in [k for k in st.miss if k not in seen_keys]:
            del st.miss[k]

    # rate cap: a guest cannot fill the security queue
    kept = []
    for a in alerts:
        if len(st.alert_times) >= MAX_ALERTS_PER_BOX_HOUR and \
                now - st.alert_times[0] < 3600:
            break
        st.alert_times.append(now)
        kept.append(a)

    st.prev_cpu = {pid: (p["start_ticks"], p["cpu_ticks"], snap["uptime_s"])
                   for pid, p in snap["procs"].items()
                   if pid in tags and p["cpu_ticks"] is not None and snap["uptime_s"]}
    totals = {"procs": len(tags), "unexpected": sum(1 for t in tags.values() if t == "unexpected"),
              "conns": 0, "guest_bytes_out": 0, "guest_bytes_in": 0,
              "host_bytes_out": 0, "host_bytes_in": 0}
    for pid in tags:
        for r in by_pid.get(pid, ()):
            totals["conns"] += 1
            for k in ("guest_bytes_out", "guest_bytes_in", "host_bytes_out", "host_bytes_in"):
                totals[k] += r[k] or 0
    row = {"box_id": box.id, "kind": box.kind, "project": box.project,
           "reported_at": _iso(now), "stale": False, "error": None,
           "baseline": baseline.source, "truncated": snap["truncated"],
           "totals": totals, "orphan_conns": orphans[:MAX_CONNS_PER_PROC],
           "tree": tree}
    return row, kept


# --- I/O: fetch from a guest, sample the host, read the DB ---------------------------

Fetcher = Callable[[Any], Awaitable[Any]]
_fetchers: dict[str, Fetcher] = {}
LiveSource = Callable[[str], list]
_live_sources: list[LiveSource] = []


def register_fetcher(kind: str, fn: Fetcher) -> None:
    """Override how a kind's snapshot is fetched (WP3 may for service boxes).
    fn(box) returns the raw snapshot dict (it is sanitised here)."""
    _fetchers[kind] = fn


def register_live_source(fn: LiveSource) -> None:
    """WP2: fn(box_id) -> [{peer_port, host, ...}] for connections the proxy
    holds open right now (names a live tunnel before its egress_events row)."""
    if fn not in _live_sources:
        _live_sources.append(fn)


async def _read_line(sock, limit: int) -> bytes:
    loop = asyncio.get_running_loop()
    buf = bytearray()
    while True:
        chunk = await loop.sock_recv(sock, 65536)
        if not chunk:
            raise ValueError("guest closed before replying")
        nl = chunk.find(b"\n")
        if nl >= 0:
            buf += chunk[:nl]
            return bytes(buf)
        buf += chunk
        if len(buf) > limit:
            raise ValueError("reply too large")


async def rpc_ps(box) -> Any:
    """Default fetch: newline JSON `{"mode":"ps"}` on the run-turn port (turn
    boxes) or svcd's port (service boxes); reply `{"type":"ps","ok":true,
    "snapshot":{...}}`. Bounded by MAX_REPLY_BYTES; the caller times it out."""
    port = boxes.PORT_SVCD if box.kind == "service" else boxes.PORT_RUNTURN
    sock = await box.transport.connect(port)
    try:
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(sock, b'{"mode":"ps"}\n')
        line = await _read_line(sock, MAX_REPLY_BYTES)
    finally:
        sock.close()
    reply = json.loads(line)
    if not isinstance(reply, dict) or reply.get("type") != "ps":
        raise ValueError("not a ps reply")
    if reply.get("ok") is not True:
        raise ValueError(clean_str(reply.get("error") or "ps failed", "error"))
    return reply.get("snapshot")


_pw_mod = None


def _procwatch():
    """The guest's parsers, reused for the host's own ss output."""
    global _pw_mod
    if _pw_mod is None:
        path = Path(settings.base_dir) / "guest" / "backend" / "procwatch.py"
        spec = importlib.util.spec_from_file_location("_jav3_procwatch_host", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _pw_mod = mod
    return _pw_mod


def host_socks_from_ss(text: str, box_list: list) -> dict[str, dict[tuple, dict]]:
    """The host kernel's TCP sockets whose peer is a box guest, per box id,
    keyed by the GUEST's view of the tuple (host's reversed)."""
    by_pair = {(b.host_ip, b.guest_ip): b.id for b in box_list}
    out: dict[str, dict[tuple, dict]] = {b.id: {} for b in box_list}
    for r in _procwatch().parse_ss(text):
        bid = by_pair.get((r["laddr"], r["raddr"]))
        if bid is None or r["lport"] is None or r["rport"] is None:
            continue
        out[bid][(r["raddr"], r["rport"], r["laddr"], r["lport"])] = {
            "state": r["state"], "bytes_sent": r["bytes_sent"],
            "bytes_received": r["bytes_received"]}
    return out


async def sample_host(box_list: list) -> dict[str, dict[tuple, dict]] | None:
    """One `ss -tinH dst 10.201.0.0/16` on the host. None when unavailable
    (not Linux, no iproute2): host-truth checks are then skipped, not faked."""
    if not sys.platform.startswith("linux") or not box_list:
        return None
    try:
        p = await asyncio.create_subprocess_exec(
            "ss", "-tinH", "dst", "10.201.0.0/16",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 3)
    except (OSError, asyncio.TimeoutError):
        return None
    if p.returncode != 0:
        return None
    return host_socks_from_ss(out[:MAX_REPLY_BYTES].decode("utf-8", "replace"), box_list)


async def _approved_services(db, box) -> set[int] | None:
    try:
        async with db.execute(
                "SELECT id FROM services WHERE status='approved' AND "
                "(box_id = ? OR id = ? OR (project_slug = ? AND placement = 'per_project')"
                " OR (placement = 'shared' AND ? = 's-shared'))",
                (box.id, box.service_id or -1, box.project or "", box.id)) as cur:
            return {r[0] for r in await cur.fetchall()}
    except Exception:  # noqa: BLE001 — no services table yet: accept jav3-svc units
        return None


async def _hostnames(db, box) -> dict[int, str]:
    names: dict[int, str] = {}
    try:
        async with db.execute(
                "SELECT peer_port, host FROM egress_events WHERE box_id = ? AND "
                "peer_port IS NOT NULL AND created_at >= datetime('now', '-15 minutes') "
                "ORDER BY id ASC LIMIT 5000", (box.id,)) as cur:
            for r in await cur.fetchall():
                if isinstance(r[0], int):
                    names[r[0]] = str(r[1])[:253]
    except Exception:  # noqa: BLE001 — pre-WP2 schema
        pass
    for fn in _live_sources:
        try:
            for c in fn(box.id) or ():
                if isinstance(c, dict) and isinstance(c.get("peer_port"), int):
                    names[c["peer_port"]] = str(c.get("host") or "")[:253] or None
        except Exception:  # noqa: BLE001
            pass
    return names


async def _inbound(db, box) -> dict[int, dict]:
    out: dict[int, dict] = {}
    try:
        async with db.execute(
                "SELECT port, SUM(bytes_in), SUM(bytes_out) FROM service_port_events "
                "WHERE box_id = ? AND closed_at IS NULL GROUP BY port", (box.id,)) as cur:
            for r in await cur.fetchall():
                out[int(r[0])] = {"bytes_in": r[1] or 0, "bytes_out": r[2] or 0}
    except Exception:  # noqa: BLE001
        pass
    return out


# --- the poller ----------------------------------------------------------------------

_task: asyncio.Task | None = None


def _pollable(box) -> bool:
    if box.kind not in ("shared", "project", "service"):
        return False
    try:
        ctl = boxes.controller(box) if box.is_shared else box.ctl
        return bool(ctl is not None and ctl.running())
    except Exception:  # noqa: BLE001
        return False


async def _fetch(box) -> dict:
    fn = _fetchers.get(box.kind, rpc_ps)
    raw = await asyncio.wait_for(fn(box), FETCH_TIMEOUT_S)
    return sanitize_snapshot(raw)


def _stale_row(box, st: BoxState, now: float | None = None) -> dict:
    if st.row is not None:
        row = dict(st.row)
    else:
        row = {"box_id": box.id, "kind": box.kind, "project": box.project,
               "reported_at": None, "baseline": st.baseline_source, "truncated": False,
               "totals": None, "orphan_conns": [], "tree": []}
    row["stale"] = True
    row["error"] = st.error
    return row


def is_stale(st: BoxState, now: float) -> bool:
    period = max(2, int(settings.vm_procwatch_seconds))
    return st.reported_at is None or now - st.reported_at > 3 * period or st.error is not None


async def poll_once(now: float | None = None) -> list[dict]:
    """One cycle over every running box. Returns the rows it produced."""
    from ..db import get_db
    from .. import security
    live = [b for b in boxes.all_boxes() if _pollable(b)]
    host = await sample_host(live)          # BEFORE the guests (see module doc)
    results = await asyncio.gather(*(_fetch(b) for b in live), return_exceptions=True)
    now = time.time() if now is None else now
    rows: list[dict] = []
    db = await get_db()
    try:
        for box, res in zip(live, results):
            st = _state.setdefault(box.id, BoxState(box.id))
            if isinstance(res, BaseException):
                st.error = clean_str(f"{type(res).__name__}: {res}", "error")
                rows.append(_stale_row(box, st, now))
                continue
            try:
                row, alerts = evaluate(
                    box, res, st, baseline=await baseline_for(db, box),
                    approved=await _approved_services(db, box),
                    host_socks=None if host is None else host.get(box.id, {}),
                    hostnames=await _hostnames(db, box), inbound=await _inbound(db, box),
                    now=now)
            except Exception as e:  # noqa: BLE001 — one bad box never stops the cycle
                log.exception("procview: evaluating %s", box.id)
                st.error = clean_str(f"evaluate: {type(e).__name__}", "error")
                rows.append(_stale_row(box, st, now))
                continue
            st.snap, st.reported_at, st.error, st.row = res, now, None, row
            rows.append(row)
            for a in alerts:
                try:
                    await security.raise_event(db, kind=a["kind"], severity=a["severity"],
                                               project=box.project, summary=a["summary"],
                                               detail=a["detail"])
                except Exception:  # noqa: BLE001
                    log.exception("procview: raising %s", a["kind"])
    finally:
        await db.close()
    for r in rows:
        publish_row(r)
    return rows


def publish_row(row: dict) -> None:
    from .. import bus
    if not bus.subscriber_count(PROCS_CHAN):
        return
    bus.publish(PROCS_CHAN, event_for(row))


def event_for(row: dict) -> dict:
    if len(json.dumps(row)) > MAX_EVENT_BYTES:
        return {"type": "box_procs_changed", "box_id": row["box_id"]}
    return {"type": "box_procs", "box": row}


async def _loop() -> None:
    while True:
        try:
            if boxes.enabled():
                await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the poller must outlive any cycle
            log.exception("procview: poll cycle failed")
        await asyncio.sleep(max(2, int(settings.vm_procwatch_seconds)))


def ensure_started() -> None:
    """Start the poller once (lazily: from the API, the SSE topic, box_up)."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        _task = asyncio.get_running_loop().create_task(_loop())
    except RuntimeError:
        _task = None


async def _box_hook(event: str, box) -> None:
    try:
        if event == "box_up":
            ensure_started()
        elif event == "box_down":
            _state.pop(box.id, None)
            from .. import bus
            bus.publish(PROCS_CHAN, {"type": "box_gone", "box_id": box.id})
    except Exception:  # noqa: BLE001 — a hook that raises would fail the box start
        pass


boxes.add_hook(_box_hook)


# --- read side --------------------------------------------------------------------

def rows(box_id: str | None = None) -> list[dict]:
    """Current rows for every known box (or one), stale ones marked."""
    now = time.time()
    out = []
    for b in boxes.all_boxes():
        if box_id and b.id != box_id:
            continue
        if b.kind not in ("shared", "project", "service"):
            continue
        st = _state.get(b.id)
        if st is None:
            st = BoxState(b.id)
        if st.row is not None and not is_stale(st, now):
            out.append(st.row)
        else:
            out.append(_stale_row(b, st, now))
    return out


def reset() -> None:
    """Tests."""
    _state.clear()
