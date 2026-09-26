"""WP1 multi-box network: the checked-in ruleset IS the renderer's output
(golden file), and its pinning rules say what the design says. The root-run
net_box.sh refuses anything it was not built to pin."""
import shutil
import subprocess

import pytest

from backend.config import settings
from backend.vm import boxes, boxnet

NFT = settings.base_dir / "vm" / "net" / "jarvis-egress-boxes.nft"
NET_BOX = settings.base_dir / "vm" / "net" / "net_box.sh"


def test_golden_file_matches_renderer():
    assert NFT.read_text() == boxnet.render_ruleset(), (
        "regenerate: python -m backend.vm.boxnet > vm/net/jarvis-egress-boxes.nft")


def test_ruleset_pins_before_established_and_drops_forward():
    text = boxnet.render_ruleset()
    gi = text.split("chain guest_input {", 1)[1].split("}", 1)[0]
    lines = [ln.strip() for ln in gi.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    est = next(i for i, ln in enumerate(lines) if ln.startswith("ct state established"))
    pins = [i for i, ln in enumerate(lines) if "!= @tap_src" in ln or "!= @tap_addr" in ln]
    assert pins and max(pins) < est                 # pins judged before conntrack
    assert lines[0] == "meta nfproto ipv6 counter drop"
    assert lines[-1] == "counter drop"
    accepts = [ln for ln in lines[est + 1:] if ln.endswith("accept")]
    assert accepts == ["udp dport 53 accept", "tcp dport 53 accept",
                       "tcp dport 8443 accept", "icmp type echo-request accept"]
    assert "iifname @guest_taps jump guest_input" in text
    fwd = text.split("chain forward {", 1)[1].split("}", 1)[0]
    assert "policy drop" in fwd and "iifname @guest_taps jump guest_forward" in fwd
    gf = text.split("chain guest_forward {", 1)[1].split("}", 1)[0]
    assert "accept" not in gf


def test_shared_tap_is_static_member():
    text = boxnet.render_ruleset()
    assert 'elements = { "jvtap0" }' in text
    assert '"jvtap0" . 10.201.0.1' in text and '"jvtap0" . 10.201.0.2' in text


def test_flag_off_ruleset_untouched():
    old = (settings.base_dir / "vm" / "net" / "jarvis-egress.nft").read_text()
    assert "guest_taps" not in old and 'iifname "jvtap0" jump guest_input' in old


def test_net_box_argv(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    boxes.registry.reset()
    b = boxes.allocate("project", project="a")
    argv = boxnet.net_box_argv("add", b)
    assert argv[:3] == ["sudo", "-n", "bash"] and argv[4:] == [
        "add", "jvtap10", "10.201.10.1", "10.201.10.2"]
    with pytest.raises(ValueError):
        boxnet.net_box_argv("flush", b)
    boxes.registry.reset()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("args", [
    ["add", "jvtap10", "10.201.10.1", "10.0.0.5"],        # LAN guest address
    ["add", "jvtap10", "10.201.11.1", "10.201.11.2"],     # another slot's addresses
    ["add", "jvtap0", "10.201.0.1", "10.201.0.2"],        # the shared tap
    ["add", "jvtap3", "10.201.3.1", "10.201.3.2"],        # below range
    ["add", "jvtap255", "10.201.255.1", "10.201.255.2"],  # above range
    ["add", "jvtap010", "10.201.10.1", "10.201.10.2"],    # leading zero
    ["add", "eth0", "10.201.10.1", "10.201.10.2"],
    ["add", "jvbr10", "10.201.10.1", "10.201.10.2"],      # bridges: pin only
    ["pin", "jvtap10;id", "10.201.10.1", "10.201.10.2"],
    ["flush", "jvtap10", "10.201.10.1", "10.201.10.2"],
])
def test_net_box_refuses(args):
    r = subprocess.run(["bash", str(NET_BOX), *args], capture_output=True, text=True)
    assert r.returncode == 2, (args, r.stdout, r.stderr)
