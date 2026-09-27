"""e2e BUG-1: a second instance on the same host must never load, flush or tear
down the default install's tap / nft table / tcpdump. Everything reaches the
root-run scripts as validated argv (sudo strips env), and every name is
instance-scoped."""
import os
import shutil
import subprocess

import pytest

from backend.config import settings
from backend.vm import boxes, boxnet

NET = settings.base_dir / "vm" / "net"
NET_UP = NET / "net_up.sh"
NFT = NET / "jarvis-egress-boxes.nft"
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _net_up(*args, path=None):
    env = {**os.environ, "PATH": f"{path}:{os.environ['PATH']}"} if path else None
    return subprocess.run(["bash", str(NET_UP), *args], capture_output=True,
                          text=True, env=env)


@pytest.fixture
def default_install(monkeypatch):
    monkeypatch.setattr(settings, "instance", "")
    monkeypatch.setattr(boxnet.config, "CONFIG_DIR", boxnet.config.DEFAULT_CONFIG_DIR)
    monkeypatch.setattr(settings, "vm_egress_tap", "jvtap0")
    monkeypatch.setattr(settings, "vm_egress_host_ip", "10.201.0.1")
    monkeypatch.setattr(settings, "vm_egress_pcap", True)


@pytest.fixture
def named(monkeypatch):
    monkeypatch.setattr(settings, "instance", "e2e")
    monkeypatch.setattr(settings, "vm_egress_tap", "jvtap9")
    monkeypatch.setattr(settings, "vm_egress_host_ip", "10.201.9.1")
    monkeypatch.setattr(settings, "vm_guest_cid", 9)
    monkeypatch.setattr(settings, "vm_egress_pcap", False)


def test_default_install_argv_is_bare(default_install):
    # the default install keeps the argv its existing sudoers line matches
    argv = boxnet.net_up_argv("down-boxes")
    assert argv[:3] == ["sudo", "-n", "bash"] and argv[4:] == ["down-boxes"]
    assert boxnet.nft_table() == "jarvis_vm"


def test_named_instance_passes_everything_in_argv(named):
    assert boxnet.net_up_argv("up-boxes")[4:] == [
        "up-boxes", "jvtap9", "10.201.9.1", "0", "jarvis_vm_e2e"]
    b = boxes.Box(id="p-a", kind="project", project="a", cid=10, tap="jvtap10",
                  host_ip="10.201.10.1", guest_ip="10.201.10.2", prefix=30,
                  mac="52:54:00:c9:00:0a", image=("main", None), mem_mb=512,
                  cpus=1, dir=settings.base_dir, runtime="kvm")
    assert boxnet.net_box_argv("add", b)[4:] == [
        "add", "jvtap10", "10.201.10.1", "10.201.10.2", "jarvis_vm_e2e"]


def test_named_instance_refuses_the_default_tap(named, monkeypatch):
    monkeypatch.setattr(settings, "vm_egress_tap", "jvtap0")
    monkeypatch.setattr(settings, "vm_egress_host_ip", "10.201.0.1")
    with pytest.raises(ValueError):
        boxnet.net_up_argv("up")


def test_tap_and_ip_must_agree(default_install, monkeypatch):
    monkeypatch.setattr(settings, "vm_egress_tap", "jvtap9")
    with pytest.raises(ValueError):
        boxnet.shared_net()


def test_config_dir_derives_an_instance(default_install, monkeypatch, tmp_path):
    monkeypatch.setattr(boxnet.config, "CONFIG_DIR", tmp_path)
    assert boxnet.nft_table().startswith("jarvis_vm_")


@needs_bash
def test_net_up_renders_instance_names():
    r = _net_up("render-nft-boxes")
    assert r.returncode == 0 and r.stdout == NFT.read_text()      # default: as checked in
    r = _net_up("render-nft-boxes", "jvtap9", "10.201.9.1", "0", "jarvis_vm_e2e")
    assert r.returncode == 0, r.stderr
    assert "table inet jarvis_vm_e2e {" in r.stdout
    assert "jvtap0" not in r.stdout and "table inet jarvis_vm\n" not in r.stdout
    assert '"jvtap9" . 10.201.9.1' in r.stdout and '"jvtap9" . 10.201.9.2' in r.stdout
    d = _net_up("render-dns-boxes", "jvtap9", "10.201.9.1", "0", "jarvis_vm_e2e")
    assert d.returncode == 0, d.stderr
    assert "interface=jvtap9" in d.stdout and "interface=jvtap*" not in d.stdout
    assert "dhcp-range" not in d.stdout and "/dns-e2e.log" in d.stdout


@needs_bash
@pytest.mark.parametrize("args", [
    ["up", "jvtap0", "10.201.0.1", "1", "jarvis_vm_e2e"],   # named on the live tap
    ["up", "jvtap9", "10.201.0.1", "1", "jarvis_vm_e2e"],   # ip not the tap's
    ["up", "eth0", "10.201.0.1", "1", "jarvis_vm"],
    ["up", "jvtap9", "10.201.9.1", "1", "other_table"],
    ["up", "jvtap9", "10.201.9.1", "yes", "jarvis_vm_e2e"],
    ["down", "jvtap9", "10.201.9.1", "1", "jarvis_vm_E2E;x"],
])
def test_net_up_refuses(args):
    r = _net_up(*args)
    assert r.returncode == 2, (args, r.stdout, r.stderr)


@needs_bash
def test_teardown_only_touches_own_taps(tmp_path):
    # down-boxes deletes the taps in ITS OWN table's set, not every jvtap*: the
    # fake nft answers only for jarvis_vm_e2e, whose set never holds jvtap0
    fake = tmp_path / "nft"
    fake.write_text('#!/bin/sh\n[ "$4" = "jarvis_vm_e2e" ] || exit 1\n'
                    'echo \'elements = { "jvtap9", "jvtap60", "jvtap61" }\'\n')
    fake.chmod(0o755)
    r = _net_up("render-own-taps", "jvtap9", "10.201.9.1", "0", "jarvis_vm_e2e",
                path=tmp_path)
    assert r.stdout.split() == ["jvtap60", "jvtap61"]
