"""The no-guest error lists every blocker at once (KVM, vsock, image, key)."""
from backend.vm import lifecycle


def test_all_blockers_listed_together(monkeypatch):
    monkeypatch.setattr(lifecycle.os.path, "exists", lambda p: False)
    monkeypatch.setattr(lifecycle, "base_built", lambda: False)
    msg = lifecycle.no_image_message()
    assert "cannot run an agent turn" in msg
    assert "/dev/kvm" in msg and "vhost-vsock" in msg and "build_base.sh" in msg
    assert "on the Pi" not in msg


def test_single_blocker_keeps_the_short_form(monkeypatch):
    monkeypatch.setattr(lifecycle, "blockers", lambda: ["no guest image"])
    real = lifecycle.os.path.exists
    monkeypatch.setattr(lifecycle.os.path, "exists",
                        lambda p: True if p == "/dev/kvm" else real(p))
    assert lifecycle.no_image_message().startswith("no golden image — build it")


import logging
import os

from backend.config import settings


def _no_kvm(monkeypatch, *, present=()):
    real = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: (p in present) if p in (
        "/dev/kvm", "/dev/vhost-vsock") else real(p))


def test_docker_only_host_does_not_list_kvm_blockers(monkeypatch):
    """/api/vm/status listed no /dev/kvm, no vsock and no guest image on a host
    that runs its turns in Docker boxes."""
    _no_kvm(monkeypatch)
    monkeypatch.setattr(lifecycle, "base_built", lambda: False)
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    assert lifecycle.docker_only_host()
    assert not [x for x in lifecycle.blockers()
                if "kvm" in x or "vsock" in x or "guest image" in x]
    st = lifecycle.GuestVM().status()
    assert st["notes"] and "Docker boxes" in st["notes"][0]
    assert "docker runtime" in lifecycle.no_image_message()
    # Docker on but boxes off: every turn still uses the shared KVM guest
    monkeypatch.setattr(settings, "vm_boxes_enabled", False)
    assert not lifecycle.docker_only_host()
    assert any("/dev/kvm" in x for x in lifecycle.blockers())
    assert lifecycle.GuestVM().status()["notes"] == []
    # a KVM-capable host keeps the KVM blockers whatever Docker says
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    _no_kvm(monkeypatch, present=("/dev/kvm",))
    assert not lifecycle.docker_only_host()
    assert any("vhost-vsock" in x for x in lifecycle.blockers())


def test_startup_line_does_not_say_turns_fail_when_docker_boxes_run_them(monkeypatch, caplog):
    from backend import main
    _no_kvm(monkeypatch)
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    with caplog.at_level(logging.WARNING, logger="jav3"):
        main._warn_missing_guest_devices()
    (line,) = [r.getMessage() for r in caplog.records if r.name == "jav3"]
    assert "Docker boxes" in line and "agent turns will fail" not in line
    caplog.clear()
    monkeypatch.setattr(settings, "docker_enabled", False)      # KVM is the only way: as before
    with caplog.at_level(logging.WARNING, logger="jav3"):
        main._warn_missing_guest_devices()
    (line,) = [r.getMessage() for r in caplog.records if r.name == "jav3"]
    assert "agent turns will fail" in line
