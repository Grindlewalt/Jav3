"""e2e BUG-5: explicitly denied hosts (project/profile deny list) must not reach
the approval queue. e2e BUG-7: egress events from a per_project service box
carry the service's id."""
import pytest

from backend.config import settings
from backend.vm import boxes


@pytest.fixture
def on(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 10**6)
    boxes.registry.reset()
    yield
    boxes.registry.reset()


async def _rows(sql):
    from backend.db import get_db
    db = await get_db()
    try:
        async with db.execute(sql) as cur:
            return [tuple(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def test_only_undecided_hosts_are_queued(on):
    from backend import egress
    from backend.db import init_db
    from backend.vm import egress_proxy
    await init_db()
    box = boxes.allocate("project", project="boxa")
    att = egress_proxy.attribute(box, ("10.201.10.2", 40000))
    await egress_proxy._record("example.net", "CONNECT", None, 0, 0, "deny",
                               "host on the project denylist", att)
    await egress_proxy._record("bad.example", "CONNECT", None, 0, 0, "deny",
                               "host on the Scoped profile denylist", att)
    assert await _rows("SELECT host FROM egress_pending") == []
    await egress_proxy._record("wikipedia.org", "CONNECT", None, 0, 0, "deny",
                               egress.NOT_LISTED, att)
    assert [r[0] for r in await _rows("SELECT host FROM egress_pending")] == ["wikipedia.org"]
    assert len(await _rows("SELECT id FROM egress_events")) == 3   # all still recorded


async def test_per_project_service_events_carry_service_id(on, monkeypatch):
    from backend.db import init_db
    from backend.vm import egress_proxy, services
    await init_db()
    box = boxes.allocate("service", project="boxa", placement="per_project")
    assert box.service_id is None
    monkeypatch.setitem(services._box_egress, box.id,
                        {"example.com": [3], "api.other.org": [4]})
    att = egress_proxy.attribute(box, ("10.201.50.2", 40000))
    await egress_proxy._record("www.example.com", "CONNECT", None, 0, 0, "allow",
                               "host in the approved service's egress_hosts", att)
    await egress_proxy._record("api.other.org", "CONNECT", None, 0, 0, "allow",
                               "host in the approved service's egress_hosts", att)
    got = await _rows("SELECT host, service_id FROM egress_events ORDER BY id")
    assert got == [("www.example.com", 3), ("api.other.org", 4)]
    assert att["service_id"] is None                  # per-connection att untouched
    # one service in the box: even a denied host is attributed to it
    monkeypatch.setitem(services._box_egress, box.id, {"example.com": [3]})
    assert services.service_for_host(box.id, "wikipedia.org") == 3


async def test_live_tunnel_names_the_process_view_host(on, monkeypatch):
    # e2e minor: a proxied connection had host null in the process view until
    # its tunnel closed; the proxy now serves the CONNECT host while it is open
    from backend.vm import egress_proxy, procview
    box = boxes.allocate("project", project="boxa")
    monkeypatch.setitem(egress_proxy._LIVE, (box.id, 40123), "speed.cloudflare.com")

    class NoDb:
        def execute(self, *a):
            raise RuntimeError("no db")
    names = await procview._hostnames(NoDb(), box)
    assert names == {40123: "speed.cloudflare.com"}
