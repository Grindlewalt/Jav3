"""Golden-image lifecycle (D1): highest-version auto-activation + staleness."""
import os
import time

import pytest

from backend.config import settings
from backend.vm import lifecycle


@pytest.fixture
def vmdir(tmp_path, monkeypatch):
    d = tmp_path / "vm"
    d.mkdir()
    monkeypatch.setattr(settings, "vm_dir", d)
    return d


def test_highest_version_wins(vmdir):
    (vmdir / "base-v1.qcow2").write_bytes(b"x")
    (vmdir / "base-v3.qcow2").write_bytes(b"x")
    (vmdir / "base-v2.qcow2").write_bytes(b"x")
    assert lifecycle._base_image().name == "base-v3.qcow2"
    assert lifecycle._active_version() == "v3"
    assert lifecycle._next_version() == 4


def test_next_version_from_empty(vmdir):
    assert lifecycle._next_version() == 1


def test_fresh_image_not_stale(vmdir):
    (vmdir / "base-v1.qcow2").write_bytes(b"x")
    meta = lifecycle._image_meta()
    assert meta["image_stale"] is False
    assert meta["image_age_days"] is not None
    assert meta["image_built_at"]


def test_old_image_is_stale(vmdir, monkeypatch):
    p = vmdir / "base-v1.qcow2"
    p.write_bytes(b"x")
    old = time.time() - (settings.vm_image_max_age_days + 5) * 86400
    os.utime(p, (old, old))
    assert lifecycle._image_meta()["image_stale"] is True


def test_status_exposes_image_fields(vmdir):
    (vmdir / "base-v2.qcow2").write_bytes(b"x")
    st = lifecycle.vm.status()
    assert st["image_version"] == "v2"
    assert "image_stale" in st and "image_built_at" in st and "rebuilding" in st
    assert st["egress"] is False        # default off
