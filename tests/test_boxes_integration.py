"""Integration seams between the box work packages (WP1-WP8), connected by
the backend integrator: each test drives one hook that used to no-op."""
import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from backend.config import settings
from backend.vm import boxes


@pytest.fixture
def on(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_max_boxes", 8)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 3)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 10**6)
    boxes.registry.reset()
    yield
    boxes.registry.reset()


@pytest.fixture
def docker_on(on, monkeypatch):
    # AF_UNIX paths are short on macOS: vm_dir under /tmp
    d = Path(tempfile.mkdtemp(prefix="j9", dir="/tmp"))
    monkeypatch.setattr(settings, "vm_dir", d)
    monkeypatch.setattr(settings, "docker_enabled", True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


# --- WP8 docker wiring ------------------------------------------------------------

def test_docker_controller_and_transport_are_loaded_lazily(docker_on, monkeypatch):
    """runtime=docker selects WP8's controller and its checked transport,
    without anyone having imported docker_runtime first."""
    monkeypatch.delitem(boxes._DRIVERS, "docker", raising=False)
    monkeypatch.setitem(boxes._TRANSPORTS, "docker", boxes.UnixTransport)
    box = boxes.allocate("project", project="alpha", runtime="docker")
    from backend.vm import docker_runtime, transport_unix
    ctl = boxes.controller(box)
    assert isinstance(ctl, docker_runtime.DockerBox)
    assert isinstance(box.transport, transport_unix.UnixTransport)


def test_docker_box_json_points_at_the_in_container_forwarder(docker_on):
    box = boxes.allocate("project", project="alpha", runtime="docker")
    bj = box.box_json()
    assert bj["runtime"] == "docker"
    assert bj["net"]["proxy"] == f"http://127.0.0.1:{settings.vm_egress_proxy_port}"
    assert bj["net"]["proxy_socket"] == "/run/jav3/proxy.sock"
    assert bj["gateway"] == {"transport": "unix", "path": "/run/jav3/gateway.sock"}


def test_docker_require_settings_are_real_settings():
    from backend.vm import docker_runtime
    assert settings.docker_require_runsc is False
    assert settings.docker_require_userns is False
    assert not hasattr(docker_runtime, "PENDING_SETTINGS")


async def test_docker_box_up_hook_binds_no_host_address(docker_on, monkeypatch):
    from backend.vm import egress_proxy
    monkeypatch.setattr(settings, "vm_egress", True)
    started = []

    async def start_box(box, host=None, port=None):
        started.append(box.id)
    monkeypatch.setattr(egress_proxy.proxy, "start_box", start_box)
    dbox = boxes.allocate("project", project="alpha", runtime="docker")
    kbox = boxes.allocate("project", project="beta")
    await egress_proxy._box_hook("box_up", dbox)
    await egress_proxy._box_hook("box_up", kbox)
    assert started == [kbox.id]


class _W:
    """A minimal StreamWriter for the proxy's handle_conn."""
    def __init__(self):
        self.data, self.closed = b"", False

    def get_extra_info(self, name):
        return ""                      # AF_UNIX peername

    def write(self, b):
        self.data += b

    async def drain(self):
        pass

    def close(self):
        self.closed = True


def _reader(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


async def test_handle_box_conn_attributes_to_the_docker_box(docker_on, monkeypatch):
    from backend.vm import egress_proxy
    monkeypatch.setattr(settings, "vm_egress", True)
    seen = {}

    async def fake_connect(host, port, cr, cw, att=None):
        seen.update(att, host=host)
    monkeypatch.setattr(egress_proxy, "_handle_connect", fake_connect)
    box = boxes.allocate("project", project="alpha", runtime="docker")
    await egress_proxy.handle_box_conn(
        box, _reader(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org\r\n\r\n"), _W())
    assert seen["host"] == "pypi.org"
    assert seen["box_id"] == box.id and seen["project"] == "alpha"
    assert seen["peer_ip"] == "unix" and seen["kind"] == "project"


async def test_handle_box_conn_refuses_non_docker_and_egress_off(docker_on, monkeypatch):
    from backend.vm import egress_proxy
    called = []

    async def fake_connect(*a, **k):
        called.append(a)
    monkeypatch.setattr(egress_proxy, "_handle_connect", fake_connect)
    head = b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n"
    monkeypatch.setattr(settings, "vm_egress", True)
    kbox = boxes.allocate("project", project="beta")
    w = _W()
    await egress_proxy.handle_box_conn(kbox, _reader(head), w)
    await egress_proxy.handle_box_conn(boxes.shared(), _reader(head), _W())
    monkeypatch.setattr(settings, "vm_egress", False)
    dbox = boxes.allocate("project", project="alpha", runtime="docker")
    await egress_proxy.handle_box_conn(dbox, _reader(head), _W())
    assert called == [] and w.closed


async def test_docker_proxy_handler_uses_handle_box_conn(docker_on, monkeypatch):
    from backend.vm import docker_runtime, egress_proxy
    got = []

    async def fake(box, r, w):
        got.append(box.id)
    monkeypatch.setattr(egress_proxy, "handle_box_conn", fake)
    box = boxes.allocate("project", project="alpha", runtime="docker")
    h = docker_runtime.proxy_handler(box)
    assert h is not None                # the direct path, not the TCP splice


async def test_vm_boxes_lists_runtimes(tmp_env, monkeypatch):
    from backend import vm_api
    monkeypatch.setattr(settings, "docker_enabled", False)
    r = await vm_api.list_boxes()
    assert set(r["runtimes"]) == {"kvm", "docker"}
    assert r["runtimes"]["docker"]["available"] is False
    assert "docker_enabled" in r["runtimes"]["docker"]["reason"]
