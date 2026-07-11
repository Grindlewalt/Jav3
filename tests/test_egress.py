"""Egress control: request/approve flow + YOLO switch."""
import pytest

from backend import egress, sandbox
from backend.db import get_db, init_db


@pytest.fixture(autouse=True)
def _no_nft(monkeypatch):
    calls = []
    async def _add(ip, port, proto): calls.append(("add", ip, port, proto))
    async def _del(ip, port, proto): calls.append(("del", ip, port, proto))
    monkeypatch.setattr(sandbox, "_nft_add", _add)
    monkeypatch.setattr(sandbox, "_nft_del", _del)
    return calls


async def test_request_dedupe_and_list(tmp_env):
    await init_db()
    r1 = await egress.file_request("proj", "1.2.3.4", 443, reason="pip")
    r2 = await egress.file_request("proj", "1.2.3.4", 443, reason="pip again")
    assert r1["id"] == r2["id"]                       # collapsed
    pend = await egress.list_requests(status="pending")
    assert len(pend) == 1 and pend[0]["host"] == "1.2.3.4"


async def test_approve_allowlists_resolved_ip(tmp_env, _no_nft):
    await init_db()
    r = await egress.file_request("proj", "9.9.9.9", 443, reason="dns")
    out = await egress.approve_request(r["id"], ttl_minutes=60)
    assert out["status"] == "approved" and out["allowed_ips"] == ["9.9.9.9"]
    # the resolved IP is now a live allowlist rule with the TTL
    idx = await sandbox.rules_index()
    assert ("9.9.9.9", 443, "tcp") in idx
    assert ("add", "9.9.9.9", 443, "tcp") in _no_nft
    # and it drops out of the pending queue
    assert await egress.list_requests(status="pending") == []


async def test_deny_leaves_no_rule(tmp_env):
    await init_db()
    r = await egress.file_request("proj", "5.5.5.5", 443)
    out = await egress.deny_request(r["id"])
    assert out["status"] == "denied"
    assert ("5.5.5.5", 443, "tcp") not in await sandbox.rules_index()


async def test_resolve_host_passthrough_ip():
    assert await egress.resolve_host("8.8.8.8") == ["8.8.8.8"]


def test_guest_resolved_reads_dns_log(tmp_path, monkeypatch):
    log = tmp_path / "dns.log"
    log.write_text(
        "query[A] pypi.org from 10.66.0.10\n"
        "reply pypi.org is 151.101.0.223\n"
        "reply pypi.org is 151.101.64.223\n"
        "reply other.com is 1.1.1.1\n")
    monkeypatch.setattr(egress, "DNS_LOG", log)
    assert egress.guest_resolved("pypi.org") == ["151.101.0.223", "151.101.64.223"]
    assert egress.guest_resolved("nope.com") == []


async def test_refresh_domains_adds_guest_ips(tmp_env, monkeypatch, _no_nft):
    await init_db()
    # an allowlisted hostname rule (dest is a domain, one known IP)
    await sandbox.add_rule("cdn.example", "1.1.1.1", 443)
    log = tmp_env / "dns.log"
    log.write_text("reply cdn.example is 1.1.1.1\nreply cdn.example is 2.2.2.2\n")
    monkeypatch.setattr(egress, "DNS_LOG", log)
    added = await egress.refresh_domains()
    assert added == 1                                 # only the new 2.2.2.2
    idx = await sandbox.rules_index()
    assert ("2.2.2.2", 443, "tcp") in idx


async def test_yolo_status_off_by_default(tmp_env, monkeypatch):
    await init_db()
    # nft unavailable in tests -> _yolo_handle returns None (off)
    async def _nft(*a, **k): return -1, "", "no nft"
    monkeypatch.setattr(sandbox, "_nft", _nft)
    st = await egress.yolo_status()
    assert st["on"] is False and st["expires_at"] is None
