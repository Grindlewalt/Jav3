"""WP1 boxes: allocation, caps, addressing, box.json and the schema contract.
Offline; no VM, no vsock."""
import asyncio
import sqlite3

import pytest

from backend.config import settings
from backend.vm import boxes


@pytest.fixture
def reg(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_max_boxes", 4)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 1)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 2250)
    monkeypatch.setattr(settings, "vm_kvm_box_overhead_mb", 0)   # mem_mb logic; overhead below
    boxes.registry.reset()
    yield boxes.registry
    boxes.registry.reset()


def test_shared_box_matches_todays_constants(reg):
    s = boxes.shared()
    assert (s.id, s.kind, s.cid, s.tap) == ("shared", "shared", 3, "jvtap0")
    assert (s.host_ip, s.guest_ip, s.prefix) == ("10.201.0.1", "10.201.0.2", 24)
    assert s.mac == "52:54:00:12:34:60"
    assert s.dir == settings.vm_dir and s.mem_mb == 768
    assert boxes.by_cid(3).id == "shared" and boxes.by_host_ip("10.201.0.1").id == "shared"


def test_project_box_addressing_and_lookup(reg):
    b = boxes.allocate("project", project="alpha")
    assert b.id == "p-alpha" and b.cid == 10
    assert (b.tap, b.host_ip, b.guest_ip, b.prefix) == (
        "jvtap10", "10.201.10.1", "10.201.10.2", 30)
    assert b.mac == "52:54:00:c9:00:0a"
    assert b.dir == settings.vm_dir / "boxes" / "p-alpha"
    assert boxes.allocate("project", project="alpha") is b      # idempotent
    assert boxes.by_cid(10) is b and boxes.by_host_ip("10.201.10.1") is b
    assert boxes.by_guest_ip("10.201.10.2") is b and boxes.by_ifname("jvtap10") is b


def test_service_and_builder_ranges(reg):
    s = boxes.allocate("service", project="alpha")
    assert s.id == "s-alpha" and s.cid == 50 and s.mem_mb == 384
    assert s.placement == "per_project"
    b = boxes.allocate("builder", variant="dev")
    assert b.id == "b-dev-90" and b.cid == 90 and b.project is None


def test_service_placements(reg):
    a = boxes.allocate("service", project="alpha", service_id=7, placement="per_service")
    assert a.id == "s-alpha-7" and a.service_id == 7
    sh = boxes.allocate("service", placement="shared", service_id=8)
    assert sh.id == "s-shared" and sh.project is None
    with pytest.raises(boxes.BoxError):
        boxes.allocate("service", project="alpha", placement="per_service")


def test_caps_project_count(reg):
    boxes.allocate("project", project="a")
    with pytest.raises(boxes.BoxCapError):
        boxes.allocate("project", project="b")


def test_caps_ram_budget(reg, monkeypatch):
    monkeypatch.setattr(settings, "vm_max_project_boxes", 3)
    boxes.allocate("project", project="a")          # 768 + 768
    with pytest.raises(boxes.BoxCapError, match="RAM"):
        boxes.allocate("project", project="b")      # + 768 > 2250
    boxes.allocate("service", project="a")          # 384 fits: 1920
    assert boxes.budget()["ram_mb_used"] == 1920


def test_ram_budget_counts_kvm_overhead(reg, monkeypatch):
    # e2e BUG-12: a KVM box costs mem_mb + ~144 MB of QEMU/firmware
    monkeypatch.setattr(settings, "vm_kvm_box_overhead_mb", 144)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 2300)
    boxes.allocate("project", project="a")          # (768+144) * 2 = 1824
    assert boxes.budget()["ram_mb_used"] == 1824
    with pytest.raises(boxes.BoxCapError, match="RAM"):
        boxes.allocate("service", project="a")      # + 384 + 144 > 2300
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 2400)   # the default
    boxes.allocate("service", project="a")          # 2352 fits
    assert boxes.budget()["ram_mb_overhead_per_kvm_box"] == 144
    assert boxes.ram_cost(512, "docker") == 512


def test_caps_box_count(reg, monkeypatch):
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 10**6)
    for i in range(3):
        boxes.allocate("service", project=f"s{i}")
    with pytest.raises(boxes.BoxCapError, match="box cap"):
        boxes.allocate("service", project="s9")


def test_desktop_variant_floor_and_budget(reg):
    d = boxes.allocate("project", project="d", variant="desktop", mem_mb=512)
    assert d.mem_mb == 1280                          # floor wins over a smaller ask
    with pytest.raises(boxes.BoxCapError, match="RAM"):
        boxes.allocate("service", project="d")       # 768 + 1280 + 384 > 2250


def test_release_frees_slot(reg):
    b = boxes.allocate("project", project="a")
    boxes.release(b.id)
    assert boxes.get("p-a") is None
    assert boxes.allocate("project", project="b").cid == 10
    with pytest.raises(boxes.BoxError):
        boxes.release("shared")


def test_bad_inputs(reg):
    for bad in ("../x", "A", "", None, "a b"):
        with pytest.raises(boxes.BoxError):
            boxes.allocate("project", project=bad)
    with pytest.raises(boxes.BoxError):
        boxes.allocate("shared")
    with pytest.raises(boxes.BoxError):
        boxes.allocate("project", project="a", variant="../evil")
    with pytest.raises(boxes.BoxError, match="docker"):
        boxes.allocate("project", project="a", runtime="docker")


def test_box_json_kvm_and_docker(reg, monkeypatch):
    b = boxes.allocate("project", project="alpha")
    j = b.box_json()
    assert j["net"] == {"guest_ip": "10.201.10.2", "prefix": 30, "gateway": "10.201.10.1",
                        "dns": "10.201.10.1", "proxy": "http://10.201.10.1:8443",
                        "mac": "52:54:00:c9:00:0a"}
    assert j["gateway"] == {"transport": "vsock", "cid": 2, "port": 5555}
    assert j["listen"]["runturn"] == {"transport": "vsock", "port": 5556}
    monkeypatch.setattr(settings, "docker_enabled", True)
    d = boxes.allocate("service", project="beta", runtime="docker")
    jd = d.box_json()
    assert d.tap == "jvbr50" and jd["runtime"] == "docker"
    assert jd["gateway"] == {"transport": "unix", "path": "/run/jav3/gateway.sock"}
    assert jd["listen"]["svcd"] == {"transport": "unix", "path": "/run/jav3/5558.sock"}
    assert boxes.by_cid(50) is None          # a docker slot is not a vsock CID


def test_kind_gates():
    assert boxes.GATEWAY_OPS["project"] >= {"model_call", "tool_broker_call", "taint_note"}
    for k in ("service", "builder"):
        assert not boxes.GATEWAY_OPS[k] & {"model_call", "tool_broker_call", "taint_note"}


def test_for_project_flag_off_is_shared(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", False)
    boxes.registry.reset()
    assert asyncio.run(boxes.for_project("alpha")).id == "shared"


def test_for_project_uses_profile(reg):
    from backend.db import init_db
    asyncio.run(init_db())
    con = sqlite3.connect(settings.db_path)
    con.execute("INSERT INTO security_profiles (name, builtin, separate_box, box_mem_mb, "
                "service_placement, box_runtime) VALUES ('Iso', 0, 1, 512, 'per_project', 'kvm')")
    con.execute("INSERT INTO security_profiles (name, builtin, service_placement, box_runtime) "
                "VALUES ('Default', 1, 'per_project', 'kvm')")
    pid = con.execute("SELECT id FROM security_profiles WHERE name='Iso'").fetchone()[0]
    con.execute("INSERT INTO projects (slug, name, path, profile_id) VALUES ('iso','iso','x',?)", (pid,))
    con.execute("INSERT INTO projects (slug, name, path) VALUES ('plain','plain','y')")
    con.commit()
    con.close()
    b = asyncio.run(boxes.for_project("iso"))
    assert b.id == "p-iso" and b.mem_mb == 512
    assert asyncio.run(boxes.for_project("plain")).id == "shared"
    assert asyncio.run(boxes.for_project(None)).id == "shared"


def test_schema_contract(tmp_env):
    from backend.db import init_db
    asyncio.run(init_db())
    asyncio.run(init_db())                       # idempotent
    con = sqlite3.connect(settings.db_path)
    cols = lambda t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")}  # noqa: E731
    assert {"profile_id", "persist_imported_at", "persist_delete_after"} <= cols("projects")
    assert "deny_hosts" in cols("egress_policy") and "hosts" in cols("egress_policy")
    assert {"peer_ip", "peer_port", "box_id", "service_id"} <= cols("egress_events")
    assert "box_id" in cols("egress_pending")
    for t in ("security_profiles", "services", "service_port_events",
              "package_catalogue", "image_variants", "image_versions"):
        assert cols(t), t
    # no default for service_placement / box_runtime on new profiles
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO security_profiles (name, box_runtime) VALUES ('x', 'kvm')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO security_profiles (name, service_placement) "
                    "VALUES ('y', 'shared')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO security_profiles (name, service_placement, box_runtime) "
                    "VALUES ('z', 'everywhere', 'kvm')")
    con.close()


def test_controller_per_box(reg):
    from backend.vm import lifecycle
    assert boxes.controller(boxes.shared()) is lifecycle.vm
    assert lifecycle.vm._dir == settings.vm_dir and lifecycle.vm._cid == 3
    b = boxes.allocate("project", project="alpha")
    ctl = boxes.controller(b)
    assert isinstance(ctl, lifecycle.GuestVM) and ctl is boxes.controller(b)
    assert ctl._dir == b.dir and ctl._cid == 10 and not ctl.running()
    row = boxes.status_json(b)
    assert row["state"] == "stopped" and row["net"]["tap"] == "jvtap10"


def test_ram_refusal_names_the_shared_reservation_and_the_way_out(reg, monkeypatch):
    """The Pi's numbers: a stopped shared VM holds 912 MB, so a 2400 MB budget
    fits two 512 MB docker boxes and refuses the third although the project
    box cap is 3. The refusal must show that arithmetic and what to do."""
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "vm_kvm_box_overhead_mb", 144)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 2400)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 3)
    monkeypatch.setattr(settings, "docker_box_mem_mb", 512)
    boxes.allocate("project", project="a", runtime="docker")
    boxes.allocate("project", project="b", runtime="docker")
    with pytest.raises(boxes.BoxCapError) as e:
        boxes.allocate("project", project="c", runtime="docker")
    msg = str(e.value)
    assert "1936 MB reserved" in msg and "shared 912" in msg and "p-a 512" in msg
    assert "+ 512 MB for this box > 2400 MB" in msg
    assert "JARVIS_VM_GUEST_RAM_BUDGET_MB" in msg and "Destroy a box" in msg
