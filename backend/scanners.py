"""Offline file scanners over what a gated run wrote or fetched.

ClamAV (signatures) and YARA (patterns) run host-side against the files the run
staged — i.e. everything it wrote or downloaded-and-kept in the workspace. Both
are deterministic signature/pattern engines: a hit is surfaced as a finding and
folds into the classifier verdict, but it is never an LLM and the agent's output
cannot steer it. Everything degrades to an empty result when a scanner (or its
signature DB / ruleset) is absent, so a dev laptop without them still runs.

Consistent with the sandbox threat model: the scanners only ever read the run's
own output files and emit typed findings; matched signature names are echoed as
data, never interpreted.
"""
import asyncio
import json
import shutil
from pathlib import Path

from .config import settings
from .staging import _staging_dir

SURICATA_RULES = Path("/var/lib/suricata/rules/suricata.rules")

# Curated committed ruleset ships in the repo; an operator can drop extra .yar
# files in data/yara/ to extend coverage offline.
_DEFAULT_YARA = settings.base_dir / "vm" / "yara" / "malware.yar"
_EXTRA_YARA_DIR = settings.data_dir / "yara"

SCAN_TIMEOUT = 180
MAX_FILE_MB = 64                    # skip absurdly large files (scan cost)


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def clamav_ready() -> bool:
    """clamscan present AND a signature DB exists (else clamscan errors out)."""
    if not have("clamscan"):
        return False
    db = Path("/var/lib/clamav")
    return db.is_dir() and any(db.glob("*.c[vl]d")) or any(db.glob("*.cud"))


def yara_rulesets() -> list[Path]:
    out = []
    if _DEFAULT_YARA.is_file():
        out.append(_DEFAULT_YARA)
    if _EXTRA_YARA_DIR.is_dir():
        out += sorted(p for p in _EXTRA_YARA_DIR.glob("*.yar") if p.is_file())
    return out


async def _run(cmd: list[str], timeout: float = SCAN_TIMEOUT):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except (FileNotFoundError, OSError):
        return None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _staged_abs(slug: str, relpaths: list[str]) -> list[Path]:
    base = _staging_dir(slug)
    out = []
    for rel in relpaths:
        p = base / rel
        try:
            if p.is_file() and p.stat().st_size <= MAX_FILE_MB * 1024 * 1024:
                out.append(p)
        except OSError:
            continue
    return out


def parse_clamscan(out: str, base: Path) -> list[dict]:
    """clamscan --infected lines: '<path>: <Signature> FOUND' -> findings."""
    hits = []
    for line in out.splitlines():
        if not line.endswith(" FOUND"):
            continue
        path, sep, rest = line.rpartition(": ")
        if not sep:
            continue
        sig = rest[:-len(" FOUND")].strip()
        try:
            rel = str(Path(path).relative_to(base))
        except ValueError:
            rel = path
        hits.append({"path": rel, "signature": sig})
    return hits


def parse_yara(out: str, base: Path) -> list[dict]:
    """yara default output: '<rule> <path>' (one per match) -> findings."""
    hits = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("warning") or line.startswith("error"):
            continue
        rule, sep, path = line.partition(" ")
        if not sep:
            continue
        path = path.strip()
        try:
            rel = str(Path(path).relative_to(base))
        except ValueError:
            rel = path
        hits.append({"rule": rule, "path": rel})
    return hits


async def clamscan(paths: list[Path], base: Path) -> list[dict]:
    if not clamav_ready() or not paths:
        return []
    r = await _run(["clamscan", "--no-summary", "--infected", "--stdout"]
                   + [str(p) for p in paths])
    if r is None:
        return []
    return parse_clamscan(r[1], base)


async def yara_scan(paths: list[Path], base: Path) -> list[dict]:
    rulesets = yara_rulesets()
    if not have("yara") or not rulesets or not paths:
        return []
    hits = []
    # yara's CLI is `yara RULES... TARGET` — extra args are read as MORE rules
    # files, not more targets, so a batch of files makes it try to compile the
    # second file as rules and abort. Scan one target file per invocation.
    for p in paths:
        for rules in rulesets:
            r = await _run(["yara", "--no-warnings", str(rules), str(p)])
            if r is not None:
                hits += parse_yara(r[1], base)
    # dedupe (rule, path)
    seen, out = set(), []
    for h in hits:
        k = (h["rule"], h["path"])
        if k not in seen:
            seen.add(k)
            out.append(h)
    return out


def suricata_ready() -> bool:
    return have("suricata") and SURICATA_RULES.is_file()


def parse_suricata_eve(text: str) -> list[dict]:
    """eve.json alert records -> deduped findings. Suricata severity: 1 = most
    severe (crit), 2/3 = advisory (warn). Untrusted signature/category strings
    are echoed as data, never interpreted."""
    seen, out = set(), []
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event_type") != "alert":
            continue
        a = ev.get("alert", {})
        sid = a.get("signature_id")
        dest = ev.get("dest_ip", "")
        key = (sid, dest)
        if key in seen:
            continue
        seen.add(key)
        sev = int(a.get("severity", 3) or 3)
        out.append({
            "signature": a.get("signature", ""), "category": a.get("category", ""),
            "severity": sev, "sev": "crit" if sev <= 1 else "warn",
            "src": ev.get("src_ip", ""), "dest": dest,
        })
    return out


async def suricata_scan(pcap_path: Path, out_dir: Path) -> list[dict]:
    """Run Suricata offline over a captured pcap; parse its eve.json alerts.
    Empty when Suricata/rules absent or the pcap is empty."""
    if not suricata_ready() or not pcap_path.is_file() or pcap_path.stat().st_size == 0:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    # -k none: the tap captures packets before the NIC computes checksums (offload),
    # so checksum validation would flag every run with bogus "invalid checksum"
    # anomalies. Disable it for offline pcap analysis.
    r = await _run(["suricata", "-r", str(pcap_path), "-l", str(out_dir),
                    "-S", str(SURICATA_RULES), "-k", "none",
                    "--runmode", "single"], timeout=180)
    if r is None:
        return []
    eve = out_dir / "eve.json"
    if not eve.is_file():
        return []
    try:
        return parse_suricata_eve(eve.read_text(errors="replace"))
    except OSError:
        return []


async def scan_staged(slug: str, relpaths: list[str]) -> dict:
    """Scan a run's staged output. Returns {clamav, yara, ran} — `ran` lists which
    engines actually executed so the console can distinguish 'clean' from 'not
    scanned'. Never raises: a scanner failure yields empty findings."""
    base = _staging_dir(slug)
    paths = _staged_abs(slug, relpaths or [])
    ran = []
    if clamav_ready():
        ran.append("clamav")
    if have("yara") and yara_rulesets():
        ran.append("yara")
    if not paths or not ran:
        return {"clamav": [], "yara": [], "ran": ran}
    cav, yar = await asyncio.gather(clamscan(paths, base), yara_scan(paths, base))
    return {"clamav": cav, "yara": yar, "ran": ran}
