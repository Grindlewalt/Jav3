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
import re
import shlex
from pathlib import Path

from .agent.tools import vm, vmexec
from .config import settings
from .db import get_db
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


async def _journal_drops(since: str) -> list[str]:
    code, out, _ = await _sudo("journalctl", "-k", "--since", since,
                               "-o", "short-iso", "--no-pager", timeout=15)
    if code != 0:
        return []
    return parse_drop_slice(out)


# ---- the gate flow ----------------------------------------------------------

async def run_gated(slug: str, command: str, timeout: float | None = None,
                    fresh: bool = True) -> dict:
    """Full monitored run. Returns report summary + run id."""
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

    status, error = "done", None
    try:
        result = await vmexec.run_in_project(slug, command, timeout=timeout)
    except Exception as e:
        status, error = "error", str(e)
        result = {"exit_status": -1, "stdout": "", "stderr": str(e), "staged": []}
    finally:
        await _stop_pcap(pcap_proc)

    dns = parse_dns_slice(_slice(DNS_LOG, dns_off))
    execs = parse_audit_slice(_slice(audit_path, audit_off))
    drops = await _journal_drops(since)
    flows = await _pcap_flows(pcap_path)
    have_pcap = pcap_path.is_file() and pcap_path.stat().st_size > 0

    # exec-log slice is preserved verbatim next to the pcap
    exec_log_path = cap_dir / f"gate-{run_id}-exec.log"
    cap_dir.mkdir(parents=True, exist_ok=True)
    exec_log_path.write_text("\n".join(execs) + ("\n" if execs else ""))

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
    }
