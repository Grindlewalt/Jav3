"""Security false alarms: egress_anomaly (SB1, 2026-10-01).

The Pi's last two cuts were false: Google's Chromium CDN (`*.gvt1.com`, entropy
4.10 and 4.11 against 3.8) was cut, and the agent's browser broke. Entropy is now
judged on the registrable domain, and not at all on a host someone put on the
allowlist. A real anomaly (a random-looking registered name, a volume spike to a
host nobody listed) still cuts, critical."""
import pytest

from backend import anomaly, egress, security
from backend import db as db_mod
from backend.config import settings


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    egress._cut.clear()
    security._pings.clear()
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


CDN = ["r11---sn-bvvbaxivnuxqjvhj5nu-nx5k.gvt1.com", "r4---sn-bvvbaxivnuxqjvhj5nu-nx5s.gvt1.com",
       "edgedl.me.gvt1.com", "rr3---sn-ab5l6nzr.googlevideo.com"]


def test_the_cdn_nodes_the_pi_cut_were_over_the_threshold_by_host():
    """The premise: judged per host, these tripped."""
    assert anomaly.entropy_bits_per_char(CDN[0]) >= settings.egress_entropy_threshold
    assert anomaly.entropy_bits_per_char(CDN[1]) >= settings.egress_entropy_threshold


@pytest.mark.parametrize("host,want", [
    ("r11---sn-bvvbaxivnuxqjvhj5nu-nx5k.gvt1.com", "gvt1.com"),
    ("edgedl.me.gvt1.com", "gvt1.com"),
    ("gvt1.com", "gvt1.com"),
    ("a.b.example.co.uk", "example.co.uk"),
    ("x.example.com.au", "example.com.au"),
    ("api.github.com", "github.com"),
    ("localhost", "localhost"),
    ("10.0.0.82", "10.0.0.82"),
    ("Files.PythonHosted.org.", "pythonhosted.org"),
])
def test_registrable_domain(host, want):
    assert anomaly.registrable_domain(host) == want


@pytest.mark.parametrize("host", CDN)
async def test_cdn_nodes_do_not_trip_entropy(db, host):
    assert await anomaly.check_host(db, "proj", host) is None


@pytest.mark.parametrize("host", ["a8f3k2q9zp1w7v4m.net", "x7k2q9v3zp1w8m4t.example",
                                  "kq3x9fj2lzv1w7m2pd.com", "a.b.xk3jd8s9qzv1w7m2p.org"])
async def test_a_random_looking_registered_name_still_trips(db, host):
    a = await anomaly.check_host(db, "proj", host)
    assert a and a["kind"] == "high_entropy"
    assert a["detail"]["domain"] == anomaly.registrable_domain(host)


async def test_a_host_on_the_projects_allowlist_is_not_judged_on_entropy(db):
    host = "a8f3k2q9zp1w7v4m.net"
    assert (await anomaly.check_host(db, "proj", host))["kind"] == "high_entropy"
    await egress.set_lists(db, "proj", allow=[host])
    assert await anomaly.check_host(db, "proj", host) is None
    # its subdomains are covered by the same entry, as the allowlist itself reads it
    assert await anomaly.check_host(db, "proj", "cdn." + host) is None
    # another project has no such entry
    assert (await anomaly.check_host(db, "other", host))["kind"] == "high_entropy"


async def test_an_allowlisted_host_is_still_judged_on_volume(db):
    """Only entropy is skipped: a listed host can still be the exfil channel."""
    await egress.set_lists(db, "proj", allow=["dump.com", "a.com", "b.com", "c.com"])
    for h in ("a.com", "b.com", "c.com"):
        await egress.record_event(db, slug="proj", host=h, bytes_out=1000, verdict="allow")
    await egress.record_event(db, slug="proj", host="dump.com",
                              bytes_out=settings.egress_volume_min_bytes * 4, verdict="allow")
    a = await anomaly.check_host(db, "proj", "dump.com")
    assert a and a["kind"] == "volume_spike"


# --- through the proxy: the false cut no longer happens, the real one still does ---------

async def _cuts(db):
    async with db.execute("SELECT kind, severity, acknowledged, quiet FROM security_events "
                          "WHERE kind = 'egress_anomaly'") as cur:
        return [tuple(r) for r in await cur.fetchall()]


async def test_the_proxy_leaves_a_cdn_alone_and_cuts_a_dga_host(db, monkeypatch):
    from backend.vm import egress_proxy

    async def no_nft(host):
        pass
    monkeypatch.setattr(egress_proxy, "_nft_drop", no_nft)
    att = {"project": "proj", "kind": "project", "box_id": "p-proj", "op_id": None,
           "conversation_id": None, "peer_ip": "10.0.0.2", "peer_port": 40000}
    for host in CDN:
        await egress_proxy._record(host, "CONNECT", None, 0, 0, "allow", "allow-by-default",
                                   att)
        assert not egress.is_cut("proj", host)
    assert await _cuts(db) == []
    await egress_proxy._record("a8f3k2q9zp1w7v4m.net", "CONNECT", None, 0, 0, "allow",
                               "allow-by-default", att)
    assert egress.is_cut("proj", "a8f3k2q9zp1w7v4m.net")
    assert await _cuts(db) == [("egress_anomaly", "critical", 0, None)]
