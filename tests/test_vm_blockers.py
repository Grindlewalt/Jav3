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
