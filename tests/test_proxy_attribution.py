"""WP2: per-box proxy listeners and attribution (DESIGN-BOXES "Proxy
attribution"; docs/boxes-contract.md G).

A box of its own is attributed by the listener it reached — never by the turn
context stack, which only the shared box still uses (residual #7). Every
egress_events row carries the guest end (peer_ip/peer_port) and the box/service
it came from, the columns WP4's process view joins on. No DNS and no outbound
network: every host used here is denied before the proxy would dial it."""
import asyncio
import json

import pytest

from backend import db as db_mod
from backend import egress
from backend.config import settings
from backend.vm import boxes
from backend.vm import egress_proxy as ep


class FakeWriter:
    def __init__(self, peer):
        self.peer = peer
        self.out = b""
        self.closed = False

    def get_extra_info(self, name, default=None):
        return self.peer if name == "peername" else default

    def write(self, data):
        self.out += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True


def reader_for(head: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(head)
    r.feed_eof()
    return r


CONNECT = b"CONNECT denied-%s.example:443 HTTP/1.1\r\nHost: x\r\n\r\n"


@pytest.fixture
async def env(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 2)
    monkeypatch.setattr(settings, "vm_max_boxes", 6)
    boxes.registry.reset()
    egress._stack.clear()
    egress._context.update(egress._EMPTY)
    egress._cut.clear()
    await db_mod.init_db()
    conn = await db_mod.get_db()
    a = boxes.allocate("project", project="alpha", mem_mb=384)
    b = boxes.allocate("project", project="beta", mem_mb=384)
    yield conn, a, b
    await conn.close()
    boxes.registry.reset()
    egress._stack.clear()
    egress._context.update(egress._EMPTY)


async def rows(db, host=None):
    q = "SELECT * FROM egress_events"
    args: tuple = ()
    if host:
        q += " WHERE host = ?"
        args = (host,)
    async with db.execute(q + " ORDER BY id", args) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def test_box_traffic_is_attributed_by_its_listener_not_the_stack(env):
    db, a, b = env
    # the shared-box context says "beta" is driving: it must not matter for
    # traffic arriving on alpha's listener (the residual #7 race, closed)
    egress.set_context("beta", "op-beta", 7)
    boxes.bind_op("op-alpha", a)
    egress.set_context("alpha", "op-alpha", 5)
    egress.set_context("beta", "op-beta-2", 8)
    w = FakeWriter((a.guest_ip, 40001))
    await ep.handle_conn(reader_for(CONNECT % b"a"), w, box=a)
    assert w.out.startswith(b"HTTP/1.1 403")
    [r] = await rows(db, "denied-a.example")
    assert r["project_slug"] == "alpha" and r["box_id"] == a.id
    assert r["peer_ip"] == a.guest_ip and r["peer_port"] == 40001
    assert r["op_id"] == "op-alpha" and r["conversation_id"] == 5   # alpha's own op
    # queued for alpha's approval, remembering the box
    [p] = await egress.list_pending(db, "alpha")
    assert p["host"] == "denied-a.example" and p["box_id"] == a.id

    w = FakeWriter((b.guest_ip, 40002))
    await ep.handle_conn(reader_for(CONNECT % b"b"), w, box=b)
    [r] = await rows(db, "denied-b.example")
    assert r["project_slug"] == "beta" and r["box_id"] == b.id and r["peer_port"] == 40002


async def test_concurrent_boxes_land_under_the_right_slug(env):
    db, a, b = env
    egress.set_context("someone-else", "op-x")
    jobs = []
    for i in range(10):
        box = a if i % 2 else b
        jobs.append(ep.handle_conn(reader_for(CONNECT % f"c{i}".encode()),
                                   FakeWriter((box.guest_ip, 41000 + i)), box=box))
    await asyncio.gather(*jobs)
    for r in await rows(db):
        i = int(r["host"].split("-c")[1].split(".")[0])
        want = a if i % 2 else b
        assert (r["project_slug"], r["box_id"], r["peer_port"]) == \
            (want.project, want.id, 41000 + i)


async def test_policy_is_the_boxes_own_project(env):
    db, a, _b = env
    await egress.set_lists(db, "alpha", deny=["pypi.org"])          # seeded on Default
    egress.set_context("beta", "op-beta")
    v, reason = await ep._authorize("pypi.org", "443", ep.attribute(a, None))
    assert v == "deny" and reason == "host on the project denylist"
    # beta (who the stack names) would have allowed it
    assert (await egress.decide(db, "beta", "pypi.org"))[0] == "allow"


async def test_a_listener_refuses_a_peer_that_is_not_its_box(env):
    db, a, b = env
    w = FakeWriter((b.guest_ip, 40100))                  # beta's address on alpha's listener
    await ep.handle_conn(reader_for(CONNECT % b"spoof"), w, box=a)
    assert w.out.startswith(b"HTTP/1.1 403")
    [r] = await rows(db, "denied-spoof.example")
    assert r["verdict"] == "deny" and "is not box" in r["reason"]
    assert await egress.list_pending(db) == []           # never trains a queue


async def test_shared_listener_uses_the_stack_and_records_shared(env):
    db, a, _b = env
    egress.set_context("gamma", "op-g", 3)
    w = FakeWriter(("10.201.0.2", 40200))
    await ep.handle_conn(reader_for(CONNECT % b"s"), w, box=None)
    [r] = await rows(db, "denied-s.example")
    assert r["project_slug"] == "gamma" and r["box_id"] == "shared"
    assert r["peer_ip"] == "10.201.0.2" and r["op_id"] == "op-g"
    # a project box's guest address arriving on the shared listener is refused
    w = FakeWriter((a.guest_ip, 40201))
    await ep.handle_conn(reader_for(CONNECT % b"s2"), w, box=None)
    [r] = await rows(db, "denied-s2.example")
    assert "belongs to box" in r["reason"]


async def test_unattributed_shared_traffic_queues_under_general(env):
    db, _a, _b = env
    await ep.handle_conn(reader_for(CONNECT % b"u"), FakeWriter(("10.201.0.2", 40300)))
    [r] = await rows(db, "denied-u.example")
    assert r["project_slug"] is None and r["box_id"] == "shared"
    [p] = await egress.list_pending(db)
    assert p["project_slug"] == egress.GENERAL and p["box_id"] == "shared"


async def test_service_box_is_deny_by_default_and_never_queues(env):
    db, _a, _b = env
    cur = await db.execute(
        "INSERT INTO projects(slug, name, path) VALUES ('alpha', 'a', '/tmp/a')")
    cur = await db.execute(
        "INSERT INTO services(project_slug, name, command, placement, status, "
        "desired_state, egress_hosts) "
        "VALUES ('alpha', 'bot', '[\"x\"]', 'per_service', 'approved', 'running', ?)",
        (json.dumps(["api.allowed.example"]),))
    sid = cur.lastrowid
    await db.commit()
    s = boxes.allocate("service", project="alpha", service_id=sid, placement="per_service",
                       mem_mb=256)
    w = FakeWriter((s.guest_ip, 40400))
    await ep.handle_conn(reader_for(CONNECT % b"svc"), w, box=s)
    [r] = await rows(db, "denied-svc.example")
    assert r["verdict"] == "deny" and r["service_id"] == sid and r["box_id"] == s.id
    assert r["project_slug"] == "alpha" and "service egress" in r["reason"]
    assert await egress.list_pending(db) == []
    att = ep.attribute(s, (s.guest_ip, 1))
    assert (await egress.decide_service(db, "alpha", sid, "api.allowed.example"))[0] == "allow"
    assert att["kind"] == "service" and att["service_id"] == sid


async def test_box_hook_starts_and_stops_a_real_listener(env, monkeypatch):
    db, a, _b = env
    monkeypatch.setattr(settings, "vm_egress", True)
    a.guest_ip = "127.0.0.1"             # the test's real peer address
    proxy = ep.EgressProxy()
    await proxy.start_box(a, host="127.0.0.1", port=0)
    assert proxy.box_listeners() == [a.id]
    port = proxy._box_servers[a.id].sockets[0].getsockname()[1]
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(CONNECT % b"real")
    await w.drain()
    assert (await r.read()).startswith(b"HTTP/1.1 403")
    w.close()
    for _ in range(50):
        if await rows(db, "denied-real.example"):
            break
        await asyncio.sleep(0.02)
    [row] = await rows(db, "denied-real.example")
    assert row["box_id"] == a.id and row["project_slug"] == "alpha"
    assert row["peer_ip"] == "127.0.0.1" and row["peer_port"] == w.get_extra_info("sockname")[1]
    await proxy.stop_box(a)
    assert proxy.box_listeners() == []


async def test_registered_box_hook_is_fail_closed_and_skips_shared(env, monkeypatch):
    _db, a, _b = env
    monkeypatch.setattr(settings, "vm_egress", True)
    started = []

    async def fake_start(box, host=None, port=None):
        started.append(box.id)
        raise OSError("address in use")
    monkeypatch.setattr(ep.proxy, "start_box", fake_start)
    assert ep._box_hook in boxes._hooks
    await ep._box_hook("box_up", boxes.shared())              # shared: existing listener
    assert started == []
    with pytest.raises(OSError):
        await ep._box_hook("box_up", a)                        # raising fails the start
    assert started == [a.id]


def test_flag_off_attribution_is_the_old_stack(monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", False)
    egress._stack.clear()
    egress.set_context("p", "op", 1)
    try:
        att = ep.attribute(None, ("10.201.0.2", 5))
        assert att["project"] == "p" and att["box_id"] is None and att["peer_port"] == 5
    finally:
        egress._stack.clear()
        egress._context.update(egress._EMPTY)
