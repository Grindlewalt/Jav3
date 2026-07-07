"""M4 gate flow: report building + capture-log parsers.

The full gate flow (fresh VM, tcpdump, nftables, auditd stream) needs the real
Pi and is verified there; these cover the pure analysis layer and the API's
refusal behaviors.
"""
import httpx
import pytest

from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.gate import (build_report, parse_audit_slice, parse_dns_slice,
                          parse_drop_slice)
from backend.main import app
from backend.memory import ensure_memory_seeds


DNS_SLICE = """\
Jul  7 21:02:11 dnsmasq[912]: 4 10.66.0.10/49152 query[A] pypi.org from 10.66.0.10
Jul  7 21:02:11 dnsmasq[912]: 4 10.66.0.10/49152 forwarded pypi.org to 10.0.0.1
Jul  7 21:02:12 dnsmasq[912]: 5 10.66.0.10/49153 query[AAAA] pypi.org from 10.66.0.10
Jul  7 21:02:13 dnsmasq[912]: 6 10.66.0.10/49154 query[A] evil.example.com from 10.66.0.10
Jul  7 21:02:14 dnsmasq[912]: 7 10.0.0.5/33333 query[A] not-the-guest.com from 10.0.0.5
Jul  7 21:02:15 dnsmasq[912]: 8 10.66.0.10/49155 query[A] pypi.org from 10.66.0.10
"""

AUDIT_SLICE = """\
type=SYSCALL msg=audit(1751920000.100:200): arch=c00000b7 syscall=221 key="jexec"
type=EXECVE msg=audit(1751920000.100:200): argc=3 a0="python3" a1="-c" a2=68656C6C6F
type=CWD msg=audit(1751920000.100:200): cwd="/workspace/demo"
type=EXECVE msg=audit(1751920000.200:201): argc=1 a0="ls"
type=PROCTITLE msg=audit(1751920000.200:201): proctitle=6C73
"""

DROP_SLICE = """\
Jul 07 21:02:20 pi kernel: jvm-egress-drop IN=jvtap0 OUT=eth0 SRC=10.66.0.10 DST=1.2.3.4 PROTO=TCP SPT=51000 DPT=443
Jul 07 21:02:21 pi kernel: jvm-egress-drop IN=jvtap0 OUT=eth0 SRC=10.66.0.10 DST=1.2.3.4 PROTO=TCP SPT=51001 DPT=443
Jul 07 21:02:22 pi kernel: jvm-host-drop IN=jvtap0 SRC=10.66.0.10 DST=10.66.0.1 PROTO=TCP SPT=51002 DPT=22
Jul 07 21:02:23 pi kernel: unrelated line
"""


def test_parse_dns_slice_unique_guest_queries():
    q = parse_dns_slice(DNS_SLICE)
    assert q == ["pypi.org (A)", "pypi.org (AAAA)", "evil.example.com (A)"]


def test_parse_audit_slice_decodes_hex_args():
    execs = parse_audit_slice(AUDIT_SLICE)
    assert execs == ["python3 -c hello", "ls"]


def test_parse_drop_slice_dedupes_and_tags_host():
    drops = parse_drop_slice(DROP_SLICE)
    assert drops == ["TCP 1.2.3.4:443", "TCP 10.66.0.1:22  [host]"]


def test_build_report_sections_and_verdict():
    result = {"exit_status": 0, "stdout": "ok", "stderr": "", "staged": ["code/x.py"]}
    rep = build_report("demo", "python3 x.py", result,
                       dns=["pypi.org (A)"], execs=["python3 x.py"],
                       drops=["TCP 1.2.3.4:443"], flows=["tcp -> 1.1.1.1.443"],
                       locked=True, fresh=True, pcap="/tmp/x.pcap")
    assert "egress lock verified" in rep
    assert "1 blocked connection attempt(s)" in rep
    assert "code/x.py" in rep and "pypi.org (A)" in rep
    assert "```stdout\nok\n```" in rep

    rep2 = build_report("demo", "x", result, [], [], [], [], locked=False,
                        fresh=False, pcap=None)
    assert "WARNING: egress lock NOT verified" in rep2
    assert "reused VM state" in rep2
    assert "no blocked connection attempts" in rep2


@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        yield c


async def test_gate_run_refusals(client):
    r = await client.post("/api/vm/gate/run",
                          json={"project": "nope", "command": "ls"})
    assert r.status_code == 404
    await client.post("/api/projects", json={"name": "Demo"})
    r = await client.post("/api/vm/gate/run",
                          json={"project": "demo", "command": "   "})
    assert r.status_code == 400


async def test_gate_report_404s(client):
    r = await client.get("/api/vm/gate/runs/999/report")
    assert r.status_code == 404
    r = await client.get("/api/vm/gate/runs")
    assert r.status_code == 200 and r.json() == []


async def test_run_gated_full_flow_mocked(client, tmp_env, monkeypatch):
    """The gate orchestration end-to-end with the VM/capture layer mocked:
    nuke -> run -> analyze -> staged report + runs row, all offline."""
    from backend import gate
    from backend.config import settings
    from backend.db import get_db
    from backend.staging import effective_read

    await client.post("/api/projects", json={"name": "Gate Demo"})
    monkeypatch.setattr(settings, "vm_dir", tmp_env / "vm")

    nuked = {"n": 0}

    async def fake_nuke(*a, **k):
        nuked["n"] += 1

    async def fake_run(slug, command, timeout=None, input=None):
        return {"exit_status": 0, "stdout": "gate-ok", "stderr": "",
                "timed_out": False, "staged": ["code/out.txt"]}

    async def fake_locked():
        return True

    async def fake_drops(since):
        return ["TCP 1.2.3.4:443"]

    async def fake_start(path):
        return None

    async def fake_stop(proc):
        return None

    async def fake_flows(path):
        return []

    monkeypatch.setattr(gate.vm, "nuke", fake_nuke)
    monkeypatch.setattr(gate.vmexec, "run_in_project", fake_run)
    monkeypatch.setattr(gate, "egress_locked", fake_locked)
    monkeypatch.setattr(gate, "_journal_drops", fake_drops)
    monkeypatch.setattr(gate, "_start_pcap", fake_start)
    monkeypatch.setattr(gate, "_stop_pcap", fake_stop)
    monkeypatch.setattr(gate, "_pcap_flows", fake_flows)

    r = await gate.run_gated("gate-demo", "python3 -c 'print(1)'", fresh=True)
    assert nuked["n"] == 1
    assert r["exit_status"] == 0 and r["status"] == "done"
    assert r["egress_locked"] is True and r["blocked_attempts"] == 1
    assert r["report"] == f"runs/gate-{r['run_id']}/report.md"

    p = effective_read("gate-demo", r["report"])
    assert p is not None
    report = p.read_text()
    assert "egress lock verified" in report
    assert "1 blocked connection attempt(s)" in report
    assert "TCP 1.2.3.4:443" in report

    db = await get_db()
    try:
        cur = await db.execute("SELECT status FROM runs WHERE id = ?", (r["run_id"],))
        row = await cur.fetchone()
    finally:
        await db.close()
    assert row["status"] == "done"
