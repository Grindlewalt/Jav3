"""M4 monitored execution — the gate flow.

Every gated run: verify the egress lock -> nuke + boot a fresh overlay ->
start captures (tcpdump on the tap, DNS-log and audit-stream offsets) ->
push the project (canonical + staged overlay) -> run -> pull (changes land
in staging as usual) -> stop captures -> analyze pcap / DNS / exec log /
firewall drops -> write a gate report (staged, plus a runs-table row).

Nothing the run produced goes live until the operator approves the staged
files; the report exists so that approval is an *informed* decision.

Host-side requirements (test Pi): jarvis-vm-net.service (tap + nftables),
jarvis-vm-dns.service (logged dnsmasq), tcpdump, passwordless sudo for the
service user. Everything degrades gracefully when absent (dev laptop) —
capture sections then say "unavailable" instead of failing the run.
"""
import asyncio
import binascii
import datetime as dt
import json
import re
from pathlib import Path

from .agent.tools import vm, vmexec
from .config import settings
from .db import get_db
from . import threatintel
from .sandbox import classify, match_sensitive, rules_index
from .staging import stage_write

TAP = "jvtap0"
DNS_LOG = Path("/var/log/jarvis-vm/dns.log")
AUDIT_STREAM = "audit-stream.log"          # under settings.vm_dir (QEMU chardev)
GUEST_IP = "10.66.0.10"
REPORT_CAP = 200                            # max lines per report section


async def _sudo(*args: str, timeout: float = 20) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "timeout"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def egress_locked() -> bool:
    """The 'lock net' step: the deny-by-default table must be loaded."""
    code, out, _ = await _sudo("nft", "list", "table", "inet", "jarvis_vm")
    return code == 0 and "guest_forward" in out


def _offset(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _slice(path: Path, offset: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read().decode(errors="replace")
    except OSError:
        return ""


# ---- pure parsers (offline-testable) --------------------------------------

def parse_dns_slice(text: str) -> list[str]:
    """dnsmasq log lines -> unique 'name (type)' queries from the guest."""
    seen, out = set(), []
    for m in re.finditer(r"query\[([A-Z]+)\] (\S+) from " + re.escape(GUEST_IP), text):
        key = f"{m.group(2)} ({m.group(1)})"
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _decode_audit_arg(v: str) -> str:
    if v.startswith('"'):
        return v.strip('"')
    try:  # unquoted args are hex-encoded
        return binascii.unhexlify(v).decode(errors="replace")
    except (binascii.Error, ValueError):
        return v


def parse_audit_slice(text: str) -> list[str]:
    """audit EXECVE records -> one 'argv joined' line per exec."""
    out = []
    for line in text.splitlines():
        if "type=EXECVE" not in line:
            continue
        args = re.findall(r"\ba\d+=(\"[^\"]*\"|\S+)", line)
        argv = [_decode_audit_arg(a) for a in args]
        if argv:
            out.append(" ".join(argv))
    return out


def execs_after_marker(execs: list[str], marker: str) -> list[str]:
    """Drop everything up to and including the run's sentinel exec.

    The fresh VM's own boot (cloud-init, `systemctl enable`, writes under
    /lib/systemd) runs as the same `agent` uid as the command, so it can't be
    told apart by user — but it all happens *before* the command. We prepend a
    unique marker exec to the command; only execs after it are the run's own,
    which keeps boot activity from tripping the behavioral rules. If the marker
    never surfaced (audit flush lag dropped it) we keep the full list rather
    than silently dropping real activity."""
    for i, e in enumerate(execs):
        if marker in e:
            return execs[i + 1:]
    return execs


_AUDIT_EVENT = re.compile(r"audit\((\d+\.\d+:\d+)\)")
_JREAD_KEY = re.compile(r'key="?(?:jread|6A72656164)"?', re.I)   # "jread" plain or hex
_PATH_NAME = re.compile(r'\bname=("[^"]*"|[0-9A-Fa-f]+|\(null\))')


def parse_audit_paths(text: str) -> list[str]:
    """auditd open/openat records keyed `jread` -> the file paths actually read.

    A read is a SYSCALL record carrying our `jread` key plus one or more PATH
    records under the same event id; we group by that id so only opens tagged by
    the guest's read watch surface (not every PATH record in the stream). Paths
    are quoted or hex-encoded exactly like EXECVE argv, so they decode the same
    way. Deduped, order-preserving; matching against sensitive globs is the
    caller's job (kept pure here)."""
    groups: dict[str, dict] = {}
    for line in text.splitlines():
        m = _AUDIT_EVENT.search(line)
        if not m:
            continue
        g = groups.setdefault(m.group(1), {"read": False, "paths": []})
        if "type=SYSCALL" in line and _JREAD_KEY.search(line):
            g["read"] = True
        if "type=PATH" in line:
            for raw in _PATH_NAME.findall(line):
                if raw == "(null)":
                    continue
                g["paths"].append(_decode_audit_arg(raw))
    seen, out = set(), []
    for g in groups.values():
        if not g["read"]:
            continue
        for p in g["paths"]:
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def parse_drop_slice(text: str) -> list[str]:
    """kernel log lines with our nft prefixes -> unique 'proto dst:port'."""
    seen, out = set(), []
    for line in text.splitlines():
        if "jvm-egress-drop" not in line and "jvm-host-drop" not in line:
            continue
        dst = re.search(r"DST=(\S+)", line)
        dpt = re.search(r"DPT=(\d+)", line)
        proto = re.search(r"PROTO=(\S+)", line)
        if dst:
            key = (f"{proto.group(1) if proto else '?'} "
                   f"{dst.group(1)}:{dpt.group(1) if dpt else '?'}"
                   + ("  [host]" if "jvm-host-drop" in line else ""))
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def parse_dns_typed(text: str) -> list[dict]:
    """dnsmasq queries from the guest -> [{name, type}] (unique, ordered)."""
    seen, out = set(), []
    for m in re.finditer(r"query\[([A-Z]+)\] (\S+) from " + re.escape(GUEST_IP), text):
        key = (m.group(2), m.group(1))
        if key not in seen:
            seen.add(key)
            out.append({"name": m.group(2), "type": m.group(1)})
    return out


def parse_dns_replies(text: str) -> dict:
    """dnsmasq 'reply <name> is <ip>' lines -> {ip: hostname}."""
    ip2host = {}
    for m in re.finditer(r"reply (\S+) is (\d+\.\d+\.\d+\.\d+)", text):
        ip2host[m.group(2)] = m.group(1)
    return ip2host


def parse_drops_counted(text: str) -> list[dict]:
    """nft drop log -> [{ip, port, proto, attempts}] aggregated per dest."""
    counts: dict[tuple, int] = {}
    for line in text.splitlines():
        if "jvm-egress-drop" not in line and "jvm-host-drop" not in line:
            continue
        dst = re.search(r"DST=(\S+)", line)
        dpt = re.search(r"DPT=(\d+)", line)
        proto = re.search(r"PROTO=(\S+)", line)
        if not dst:
            continue
        key = (dst.group(1), int(dpt.group(1)) if dpt else 0,
               (proto.group(1) if proto else "tcp").lower())
        counts[key] = counts.get(key, 0) + 1
    return [{"ip": ip, "port": port, "proto": proto, "attempts": n}
            for (ip, port, proto), n in counts.items()]


def pcap_bytes(text: str) -> list[dict]:
    """`tcpdump -nn -q -r` output -> per-remote-peer delivered byte totals.

    Only established (allowlisted) flows carry payload; SSH to the guest is
    already filtered out of the capture. Payload bytes only (headers excluded)
    — an honest floor on what was actually delivered."""
    agg: dict[tuple, list] = {}
    rx = re.compile(r"IP (\d+\.\d+\.\d+\.\d+)\.(\d+) > (\d+\.\d+\.\d+\.\d+)\.(\d+): "
                    r"(tcp|UDP,? ?\w*) ?(?:length )?(\d+)?", re.I)
    for line in text.splitlines():
        m = rx.search(line)
        if not m:
            continue
        sip, sport, dip, dport, proto, ln = m.groups()
        length = int(ln) if ln else 0
        proto = "udp" if proto.lower().startswith("udp") else "tcp"
        if sip == GUEST_IP:                 # guest -> remote (upload)
            key = (dip, int(dport), proto)
            agg.setdefault(key, [0, 0])[1] += length
        elif dip == GUEST_IP:               # remote -> guest (download)
            key = (sip, int(sport), proto)
            agg.setdefault(key, [0, 0])[0] += length
    return [{"ip": ip, "port": port, "proto": proto,
             "bytes_down": d, "bytes_up": u}
            for (ip, port, proto), (d, u) in agg.items()]


async def _pcap_dump(path: Path) -> str:
    if not path.is_file() or path.stat().st_size == 0:
        return ""
    proc = await asyncio.create_subprocess_exec(
        "tcpdump", "-nn", "-q", "-r", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


def _cap(lines: list[str]) -> list[str]:
    if len(lines) > REPORT_CAP:
        return lines[:REPORT_CAP] + [f"... ({len(lines) - REPORT_CAP} more)"]
    return lines


def build_report(slug: str, command: str, result: dict, dns: list[str],
                 execs: list[str], drops: list[str], flows: list[str],
                 locked: bool, fresh: bool, pcap: str | None) -> str:
    def sec(title, lines, empty):
        body = "\n".join(f"- {l}" for l in _cap(lines)) if lines else f"_{empty}_"
        return f"## {title}\n{body}\n"

    verdict = []
    verdict.append("egress lock verified" if locked
                   else "**WARNING: egress lock NOT verified**")
    verdict.append("fresh VM (nuked before run)" if fresh else "reused VM state")
    verdict.append(f"{len(drops)} blocked connection attempt(s)" if drops
                   else "no blocked connection attempts")
    verdict.append(f"{len(dns)} DNS name(s) looked up" if dns else "no DNS lookups")

    out = result.get("stdout", "")[-2000:]
    err = result.get("stderr", "")[-2000:]
    return (
        f"# Gate report — `{slug}`\n\n"
        f"command: `{command}`\n"
        f"exit: {result.get('exit_status')}"
        f"{' (timed out)' if result.get('timed_out') else ''}\n\n"
        f"**Verdict:** {'; '.join(verdict)}.\n\n"
        + sec("Staged file changes", result.get("staged") or [], "none")
        + sec("DNS lookups (guest)", dns, "none")
        + sec("Blocked egress attempts (nftables)", drops, "none")
        + sec("Network flows seen on tap (non-SSH)", flows, "none / capture unavailable")
        + sec("Exec log (guest auditd)", execs, "unavailable")
        + (f"\npcap: `{pcap}`\n" if pcap else "")
        + f"\n```stdout\n{out}\n```\n```stderr\n{err}\n```\n"
    )


# ---- capture helpers --------------------------------------------------------

async def _start_pcap(path: Path) -> asyncio.subprocess.Process | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "tcpdump", "-i", TAP, "-U", "-w", str(path),
            f"not (port 22 and host {GUEST_IP})",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return None
    await asyncio.sleep(0.5)
    if proc.returncode is not None:        # died instantly (no sudo/tcpdump/tap)
        return None
    return proc


async def _stop_pcap(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    await asyncio.sleep(0.5)               # let -U flush the tail
    code, _, _ = await _sudo("pkill", "-INT", "-P", str(proc.pid))
    if code != 0:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        proc.kill()


async def _pcap_flows(path: Path) -> list[str]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    proc = await asyncio.create_subprocess_exec(
        "tcpdump", "-nn", "-r", str(path), "-q",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    seen, flows = set(), []
    for line in out.decode(errors="replace").splitlines():
        m = re.search(r"IP \S+ > (\S+?): (\w+)", line)
        if m:
            key = f"{m.group(2)} -> {m.group(1)}"
            if key not in seen:
                seen.add(key)
                flows.append(key)
    return flows


async def _drop_text(since: str) -> str:
    code, out, _ = await _sudo("journalctl", "-k", "--since", since,
                               "-o", "short-iso", "--no-pager", timeout=15)
    return out if code == 0 else ""


async def _journal_drops(since: str) -> list[str]:
    return parse_drop_slice(await _drop_text(since))


def parse_render_attempts(stdout: str) -> list[dict]:
    """Pull the beacon-catcher harness's attempt log out of the run stdout."""
    for line in stdout.splitlines():
        if line.startswith("JARVIS_RENDER "):
            try:
                return json.loads(line[len("JARVIS_RENDER "):]).get("attempts", [])
            except ValueError:
                return []
    return []


def _build_evidence(run_id: int, slug: str, command: str, result: dict,
                    locked: bool, fresh: bool, dns_text: str, execs: list[str],
                    since_drops: list[dict], pcap_text: str,
                    render: dict | None = None,
                    read_paths: list[str] | None = None) -> dict:
    """Raw capture assembled into one record. No verdict, no severity — that is
    sandbox.classify()'s job against the live allowlist."""
    ip2host = parse_dns_replies(dns_text)

    # Real sensitive-read detection: auditd tagged these paths as opened for
    # read (key `jread`); match them against the operator's sensitive globs here
    # so classify() sees genuine reads, not just argv/staged approximations.
    sensitive = []
    seen = set()
    for p in (read_paths or []):
        g = match_sensitive(p)
        if g and p not in seen:
            seen.add(p)
            sensitive.append({"path": p, "glob": g})

    # The tap sees the guest's outbound SYNs even for connections nftables
    # drops, so packet-presence does NOT mean "allowed". A connection was only
    # actually delivered if data came BACK (bytes_down > 0); a dropped SYN gets
    # no reply. Everything else is an attempt. Gateway DNS/DHCP is infra noise.
    def _infra(ip, port):
        return ip == "10.66.0.1" and port in (53, 67, 68)

    peers = pcap_bytes(pcap_text)
    flows = [f for f in peers
             if f["bytes_down"] > 0 and not _infra(f["ip"], f["port"])]
    for f in flows:
        f["host"] = ip2host.get(f["ip"])
    delivered_keys = {(f["ip"], f["port"], f["proto"]) for f in flows}

    blocked = []
    for b in since_drops:
        if _infra(b["ip"], b["port"]):
            continue
        if (b["ip"], b["port"], b["proto"]) in delivered_keys:
            continue                        # actually got through — not blocked
        b["host"] = ip2host.get(b["ip"])
        blocked.append(b)
    return {
        "run_id": run_id, "project": slug, "command": command,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "exit_status": result.get("exit_status"),
        "timed_out": bool(result.get("timed_out")),
        "egress_locked": locked, "fresh": fresh,
        "flows": flows, "blocked": blocked, "dns": parse_dns_typed(dns_text),
        "execs": execs, "sensitive": sensitive, "staged": result.get("staged", []),
        "render": render,
    }


# ---- the gate flow ----------------------------------------------------------

async def run_gated(slug: str, command: str, timeout: float | None = None,
                    fresh: bool = True, input: str | None = None,
                    render_of: str | None = None) -> dict:
    """Full monitored run. Returns report summary + run id.

    `input` is fed to the command's stdin (used to stream the render harness).
    `render_of` marks this as a beacon-catcher render of that artifact path;
    the command's stdout is parsed for the harness's attempt log and folded
    into the evidence."""
    project = settings.projects_dir / slug
    if not (project / "project.md").exists():
        raise LookupError(f"no such project: {slug}")

    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id FROM projects WHERE slug = ?", (slug,))
        row = await cur.fetchone()
        if not row:
            raise LookupError(f"project not in db: {slug}")
        project_id = row["id"] if not isinstance(row, tuple) else row[0]
        cur = await db.execute(
            "INSERT INTO runs (project_id, status) VALUES (?, 'running')",
            (project_id,))
        run_id = cur.lastrowid
        await db.commit()
    finally:
        await db.close()

    locked = await egress_locked()
    if fresh:
        await vm.nuke()

    cap_dir = settings.vm_dir / "captures"
    pcap_path = cap_dir / f"gate-{run_id}.pcap"
    audit_path = settings.vm_dir / AUDIT_STREAM
    dns_off, audit_off = _offset(DNS_LOG), _offset(audit_path)
    since = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pcap_proc = await _start_pcap(pcap_path)

    # A unique sentinel exec prepended to the command so we can separate the
    # run's own execs from the fresh VM's boot activity (both run as `agent`).
    marker = f"JARVISGATEMARK{run_id}"
    run_command = f"/bin/echo {marker} >/dev/null 2>&1 ; {command}"

    status, error = "done", None
    try:
        result = await vmexec.run_in_project(slug, run_command, timeout=timeout,
                                             input=input)
    except Exception as e:
        status, error = "error", str(e)
        result = {"exit_status": -1, "stdout": "", "stderr": str(e), "staged": []}
    finally:
        await _stop_pcap(pcap_proc)

    # let auditd flush the tail of the run to the host stream before slicing
    # (execve/openat records lag the process exit by up to a second or two).
    await asyncio.sleep(2)

    dns_text = _slice(DNS_LOG, dns_off)
    dns = parse_dns_slice(dns_text)
    audit_text = _slice(audit_path, audit_off)
    execs = execs_after_marker(parse_audit_slice(audit_text), marker)
    read_paths = parse_audit_paths(audit_text)
    drops = await _journal_drops(since)
    flows = await _pcap_flows(pcap_path)
    have_pcap = pcap_path.is_file() and pcap_path.stat().st_size > 0

    # exec-log slice is preserved verbatim next to the pcap
    cap_dir.mkdir(parents=True, exist_ok=True)
    exec_log_path = cap_dir / f"gate-{run_id}-exec.log"
    exec_log_path.write_text("\n".join(execs) + ("\n" if execs else ""))

    render = None
    if render_of is not None:
        render = {"artifact": render_of,
                  "attempts": parse_render_attempts(result.get("stdout", ""))}

    # structured evidence for the review console (raw capture, no verdict yet)
    evidence = _build_evidence(
        run_id, slug, command, result, locked, fresh,
        dns_text, execs, since_drops=parse_drops_counted(await _drop_text(since)),
        pcap_text=await _pcap_dump(pcap_path), render=render, read_paths=read_paths)
    evidence_path = cap_dir / f"gate-{run_id}-evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=1))

    # Deterministic verdict, same classifier the review console uses. It is
    # rule-based arithmetic over host-side captures (never steered by agent
    # output), so it is safe to hand back to the agent — closing the feedback
    # loop lets the agent react in-turn ("my run tried to beacon somewhere,
    # fix the code / ask the operator") instead of only the human seeing it.
    # Approval authority stays with the operator regardless.
    c = classify(evidence, await rules_index(), threatintel.load())

    report = build_report(slug, command, result, dns, execs, drops, flows,
                          locked, fresh, str(pcap_path) if have_pcap else None)
    report_rel = f"runs/gate-{run_id}/report.md"
    stage_write(slug, report_rel, report.encode())

    db = await get_db()
    try:
        await db.execute(
            "UPDATE runs SET status = ?, exec_log_path = ?, net_log_path = ? "
            "WHERE id = ?",
            (status if not error else "error", str(exec_log_path),
             str(pcap_path) if have_pcap else None, run_id))
        await db.commit()
    finally:
        await db.close()

    return {
        "run_id": run_id, "status": status, "error": error,
        "exit_status": result.get("exit_status"),
        "staged": result.get("staged", []), "report": report_rel,
        "egress_locked": locked, "dns_lookups": len(dns),
        "blocked_attempts": len(drops), "execs_logged": len(execs),
        "verdict": c["verdict"], "verdict_rule": c["rule"],
        "headline": c["headline"],
        "blocked": [f"{b.get('host') or b['ip']}:{b['port']}/{b['proto']} "
                    f"({b['attempts']} attempt{'s' if b['attempts'] != 1 else ''})"
                    for b in evidence["blocked"]],
    }
