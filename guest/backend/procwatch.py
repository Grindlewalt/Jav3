"""Guest process + socket snapshot for the host's Persistent view (WP4).

Pure stdlib, no package-relative imports: the same file ships in the turn
package (backend/procwatch.py, called by server.py's `mode:"ps"`) and in the
service package (WP3's svcd calls `snapshot()` the same way). One entry point:

    snapshot(root="/", self_pid=None) -> dict       (shape: SNAPSHOT_V below)

It reads /proc/<pid>/{stat,status,cmdline,cgroup,exe,fd} and
/proc/net/{tcp,tcp6,udp,udp6}, and asks `ss -tinpHe` (iproute2's view of the
kernel's tcp_info) for per-socket bytes_sent / bytes_received.

Everything here is REPORTED, never trusted: the host re-validates, caps and
sanitises every field (backend/vm/procview.py), and cross-checks connections
against what its own proxy and kernel saw. The caps below only keep an honest
guest from producing a report the host would throw away anyway.

Snapshot (v1):
  {"v": 1, "boot_id", "self_pid", "uptime_s", "clk_tck", "page_size",
   "procs": [{"pid", "ppid", "uid", "user", "comm", "exe", "cmd", "cgroup",
              "unit", "kthread", "state", "rss", "cpu_ticks", "start_ticks",
              "inodes": [socket inode, ...]}],
   "socks": [{"proto", "laddr", "lport", "raddr", "rport", "state", "inode",
              "bytes_sent", "bytes_received"}],
   "truncated": bool, "errors": [str]}
"""
import ipaddress
import os
import re
import subprocess

SNAPSHOT_V = 1
MAX_PROCS = 2048
MAX_SOCKS = 4096
MAX_FDS_PER_PROC = 1024
MAX_STR = 512
MAX_SS_BYTES = 4 * 1024 * 1024
SS_TIMEOUT_S = 2.0
PF_KTHREAD = 0x00200000

TCP_STATES = {
    "01": "ESTAB", "02": "SYN-SENT", "03": "SYN-RECV", "04": "FIN-WAIT-1",
    "05": "FIN-WAIT-2", "06": "TIME-WAIT", "07": "CLOSE", "08": "CLOSE-WAIT",
    "09": "LAST-ACK", "0A": "LISTEN", "0B": "CLOSING",
}
UDP_STATES = {"01": "ESTAB", "07": "UNCONN"}


def _clip(s: str, n: int = MAX_STR) -> str:
    return s if len(s) <= n else s[:n]


def _read(path: str, limit: int = 65536) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return None


# --- /proc/<pid>/* -------------------------------------------------------------

def parse_stat(text: str) -> dict | None:
    """/proc/<pid>/stat. comm may hold spaces and ')' so split on the LAST ')'."""
    try:
        lp, rp = text.index("("), text.rindex(")")
        pid = int(text[:lp].strip())
        comm = text[lp + 1:rp]
        f = text[rp + 2:].split()
        return {"pid": pid, "comm": _clip(comm, 64), "state": f[0][:1],
                "ppid": int(f[1]), "flags": int(f[6]),
                "cpu_ticks": int(f[11]) + int(f[12]),
                "start_ticks": int(f[19]), "rss_pages": int(f[21])}
    except (ValueError, IndexError):
        return None


def parse_status_uid(text: str) -> int | None:
    for line in text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            try:
                return int(parts[1])
            except (IndexError, ValueError):
                return None
    return None


def parse_cgroup(text: str) -> str:
    """The unified (v2) path, else the name=systemd v1 path, else ''."""
    v1 = ""
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        if parts[0] == "0" and parts[1] == "":
            return _clip(parts[2])
        if parts[1] == "name=systemd":
            v1 = parts[2]
    return _clip(v1)


_UNIT_SUFFIXES = (".service", ".scope")


def unit_of(cgroup: str) -> str:
    """The innermost systemd unit a cgroup path names ('' if none): the
    deepest component ending in .service or .scope."""
    for comp in reversed([c for c in cgroup.split("/") if c]):
        if comp.endswith(_UNIT_SUFFIXES):
            return comp
    return ""


def parse_cmdline(raw: bytes) -> str:
    parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
    return _clip(" ".join(parts))


def socket_inodes(fd_dir: str) -> list[int]:
    out = []
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return out
    for name in names[:MAX_FDS_PER_PROC]:
        try:
            target = os.readlink(os.path.join(fd_dir, name))
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            try:
                out.append(int(target[8:-1]))
            except ValueError:
                pass
    return out


# --- /proc/net/* ---------------------------------------------------------------

def _hex_addr(h: str) -> str:
    """Kernel hex address -> text. IPv4 is one little-endian 32-bit word;
    IPv6 is four of them. IPv4-mapped IPv6 is shown as plain IPv4."""
    b = bytes.fromhex(h)
    if len(b) == 4:
        return str(ipaddress.IPv4Address(b[::-1]))
    if len(b) == 16:
        words = b"".join(b[i:i + 4][::-1] for i in range(0, 16, 4))
        a = ipaddress.IPv6Address(words)
        return str(a.ipv4_mapped) if a.ipv4_mapped else str(a)
    raise ValueError("bad address length")


def parse_proc_net(text: str, proto: str) -> list[dict]:
    """/proc/net/{tcp,tcp6,udp,udp6} rows -> sockets (inode 0 = no owner,
    e.g. TIME-WAIT)."""
    states = UDP_STATES if proto.startswith("udp") else TCP_STATES
    out = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 10:
            continue
        try:
            la, lp = f[1].split(":")
            ra, rp = f[2].split(":")
            out.append({"proto": proto, "laddr": _hex_addr(la), "lport": int(lp, 16),
                        "raddr": _hex_addr(ra), "rport": int(rp, 16),
                        "state": states.get(f[3].upper(), f[3].upper()[:8]),
                        "inode": int(f[9])})
        except (ValueError, IndexError):
            continue
        if len(out) >= MAX_SOCKS:
            break
    return out


# --- ss -tinpHe ----------------------------------------------------------------

_SS_INO = re.compile(r"\bino:(\d+)")
_SS_BYTES = re.compile(r"\b(bytes_sent|bytes_received):(\d+)")


def split_hostport(s: str) -> tuple[str, int | None]:
    """'10.0.0.1:443' / '[::1]:22' / '*:22' / '10.0.0.1%eth0:53' -> (addr, port)."""
    host, _, port = s.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    host = host.split("%", 1)[0]
    try:
        p = int(port)
    except ValueError:
        p = None
    if host.startswith("::ffff:") and "." in host:
        host = host[7:]
    return host, p


def parse_ss(text: str) -> list[dict]:
    """`ss -tinpHe` (no header). A socket line starts in column 0; its tcp_info
    continuation line is indented. Returns [{laddr, lport, raddr, rport,
    state, inode, pids, bytes_sent, bytes_received}]."""
    out: list[dict] = []
    cur = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t":
            if cur is not None:
                for k, v in _SS_BYTES.findall(line):
                    cur[k] = int(v)
            continue
        f = line.split()
        if len(f) < 5:
            cur = None
            continue
        la, lp = split_hostport(f[3])
        ra, rp = split_hostport(f[4])
        m = _SS_INO.search(line)
        cur = {"state": f[0][:12], "laddr": la, "lport": lp, "raddr": ra, "rport": rp,
               "inode": int(m.group(1)) if m else None,
               "pids": [int(p) for p in re.findall(r"pid=(\d+)", line)][:16],
               "bytes_sent": None, "bytes_received": None}
        for k, v in _SS_BYTES.findall(line):
            cur[k] = int(v)
        out.append(cur)
        if len(out) >= MAX_SOCKS:
            break
    return out


def run_ss() -> str:
    try:
        p = subprocess.run(["ss", "-tinpHe"], capture_output=True,
                           timeout=SS_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout[:MAX_SS_BYTES].decode("utf-8", "replace")


def merge_bytes(socks: list[dict], ss_rows: list[dict]) -> None:
    """Attach ss's per-socket byte counters to /proc/net sockets: by inode
    when ss printed one, else by the 4-tuple."""
    by_ino = {r["inode"]: r for r in ss_rows if r.get("inode")}
    by_tuple = {(r["laddr"], r["lport"], r["raddr"], r["rport"]): r for r in ss_rows}
    for s in socks:
        r = by_ino.get(s["inode"]) if s["inode"] else None
        if r is None and s["proto"].startswith("tcp"):
            r = by_tuple.get((s["laddr"], s["lport"], s["raddr"], s["rport"]))
        s["bytes_sent"] = r.get("bytes_sent") if r else None
        s["bytes_received"] = r.get("bytes_received") if r else None


# --- the snapshot --------------------------------------------------------------

def _user(uid: int | None) -> str:
    if uid is None:
        return ""
    try:
        import pwd
        return _clip(pwd.getpwuid(uid).pw_name, 32)
    except (KeyError, ImportError):
        return str(uid)


def _exe(pdir: str) -> str:
    try:
        return _clip(os.readlink(os.path.join(pdir, "exe")))
    except OSError:
        return ""


def snapshot(root: str = "/", self_pid: int | None = None,
             ss_text: str | None = None) -> dict:
    """Everything the host's process view needs from this guest, in one dict.
    `root` and `ss_text` exist for tests (a fixture /proc and canned ss)."""
    proc = os.path.join(root, "proc")
    errors: list[str] = []
    truncated = False
    procs: list[dict] = []
    try:
        pids = sorted(int(n) for n in os.listdir(proc) if n.isdigit())
    except OSError as e:
        return {"v": SNAPSHOT_V, "procs": [], "socks": [], "truncated": False,
                "errors": [f"listdir /proc: {e.strerror}"]}
    if len(pids) > MAX_PROCS:
        pids, truncated = pids[:MAX_PROCS], True
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    for pid in pids:
        pdir = os.path.join(proc, str(pid))
        raw = _read(os.path.join(pdir, "stat"), 4096)
        st = parse_stat(raw.decode("utf-8", "replace")) if raw else None
        if st is None:
            continue                      # exited between listdir and read
        status = _read(os.path.join(pdir, "status"), 16384) or b""
        uid = parse_status_uid(status.decode("utf-8", "replace"))
        cg = parse_cgroup((_read(os.path.join(pdir, "cgroup"), 8192) or b"")
                          .decode("utf-8", "replace"))
        kthread = bool(st["flags"] & PF_KTHREAD)
        procs.append({
            "pid": pid, "ppid": st["ppid"], "uid": uid, "user": _user(uid),
            "comm": st["comm"], "state": st["state"],
            "exe": "" if kthread else _exe(pdir),
            "cmd": "" if kthread else parse_cmdline(
                _read(os.path.join(pdir, "cmdline"), 8192) or b""),
            "cgroup": cg, "unit": unit_of(cg), "kthread": kthread,
            "rss": st["rss_pages"] * page, "cpu_ticks": st["cpu_ticks"],
            "start_ticks": st["start_ticks"],
            "inodes": [] if kthread else socket_inodes(os.path.join(pdir, "fd")),
        })
    socks: list[dict] = []
    for proto in ("tcp", "tcp6", "udp", "udp6"):
        raw = _read(os.path.join(proc, "net", proto), 4 * 1024 * 1024)
        if raw is None:
            continue
        socks.extend(parse_proc_net(raw.decode("ascii", "replace"), proto))
    if len(socks) > MAX_SOCKS:
        socks, truncated = socks[:MAX_SOCKS], True
    try:
        merge_bytes(socks, parse_ss(run_ss() if ss_text is None else ss_text))
    except Exception as e:  # noqa: BLE001 — bytes are optional; never fail the report
        errors.append(f"ss: {type(e).__name__}")
    up = _read(os.path.join(proc, "uptime"), 128)
    boot = _read(os.path.join(proc, "sys", "kernel", "random", "boot_id"), 128)
    try:
        uptime = float(up.split()[0]) if up else None
    except (ValueError, IndexError):
        uptime = None
    return {"v": SNAPSHOT_V,
            "boot_id": boot.decode("ascii", "replace").strip()[:64] if boot else "",
            "self_pid": os.getpid() if self_pid is None else self_pid,
            "uptime_s": uptime,
            "clk_tck": os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100,
            "page_size": page,
            "procs": procs, "socks": socks, "truncated": truncated, "errors": errors}


# --- kill one process the host's snapshot named ---------------------------------

SIGNALS = {"TERM": 15, "KILL": 9}


def kill_pid(pid, exe, start_ticks=None, cmd=None, sig="TERM", root="/",
             self_pid=None, _kill=None) -> dict:
    """Signal `pid` only if it still is the process the host saw in its last ps
    snapshot: same program (exe) and the same start time (`start_ticks`), or,
    when the host has no start time, the same command line. A pid can be reused
    within seconds: a mismatch is refused, never killed ("changed"); a pid that
    is gone is "gone". PID 1, this server and kernel threads are refused.
    Returns {"ok": True, "pid": n, "sig": name} or {"ok": False, "why": code,
    "error": text}."""
    def no(why: str, text: str) -> dict:
        return {"ok": False, "why": why, "error": text}
    name = str(sig or "TERM").upper().removeprefix("SIG")
    if name not in SIGNALS:
        return no("bad_signal", f"signal must be one of {', '.join(SIGNALS)}")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 2 or pid > 4194304:
        return no("bad_pid", "not a killable process id")
    me = os.getpid() if self_pid is None else self_pid
    if pid == me:
        return no("protected", "that is this box's own agent server")
    if not isinstance(exe, str) or not exe:
        return no("bad_args", "the program path is required")
    if start_ticks is None and not cmd:
        return no("bad_args", "a start time or a command line is required")
    pdir = os.path.join(root, "proc", str(pid))
    raw = _read(os.path.join(pdir, "stat"), 4096)
    st = parse_stat(raw.decode("utf-8", "replace")) if raw else None
    if st is None:
        return no("gone", f"process {pid} is no longer running")
    if st["flags"] & PF_KTHREAD:
        return no("protected", "that is a kernel thread")
    live_exe = _exe(pdir)
    if live_exe != exe:
        return no("changed", f"pid {pid} is now {live_exe or 'another program'}, "
                             f"not {exe}: not killing it")
    if start_ticks is not None:
        if st["start_ticks"] != start_ticks:
            return no("changed", f"pid {pid} was started again since the snapshot: "
                                 "not killing it")
    else:
        live_cmd = parse_cmdline(_read(os.path.join(pdir, "cmdline"), 8192) or b"")
        if live_cmd != cmd:
            return no("changed", f"pid {pid} runs a different command line now: "
                                 "not killing it")
    try:
        (_kill or os.kill)(pid, SIGNALS[name])
    except ProcessLookupError:
        return no("gone", f"process {pid} is no longer running")
    except PermissionError:
        return no("denied", f"not allowed to signal process {pid}")
    return {"ok": True, "pid": pid, "sig": name}
