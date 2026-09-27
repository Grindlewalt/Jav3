"""e2e BUG-11: a profile's new box_image was ignored by an allocated project box
until it was destroyed, and /api/vm/boxes image.version was always null."""
import pytest

from backend.config import settings
from backend.vm import boxes, images


@pytest.fixture
def on(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 10**6)
    boxes.registry.reset()
    yield
    boxes.registry.reset()


class _Ctl:
    def __init__(self, running):
        self._r = running
        self.pid = None
        self.booted_at = None
        self.inflight = 0

    def running(self):
        return self._r


def test_stopped_box_follows_the_profile_image(on):
    b = boxes.allocate("project", project="a", variant="main")
    assert boxes.follow_profile_image(b, "e2epip") and b.image == ("e2epip", None)


def test_running_box_waits_and_says_restart_needed(on, monkeypatch):
    b = boxes.allocate("project", project="a", variant="e2epip")
    b.ctl = _Ctl(True)
    b.booted_image = ("e2epip", 1)
    monkeypatch.setattr(images, "active_version", lambda v: 2)
    assert not boxes.follow_profile_image(b, "main")
    assert b.image[0] == "e2epip"
    row = boxes.status_json(b)
    assert row["image"] == {"variant": "e2epip", "version": 1}     # what it runs
    assert row["restart_needed"] is True                          # v2 is active
    b.ctl = _Ctl(False)
    row = boxes.status_json(b)
    assert row["image"]["version"] == 2 and row["restart_needed"] is False


def test_ram_floor_blocks_an_in_place_switch(on):
    b = boxes.allocate("project", project="a", variant="main", mem_mb=512)
    assert not boxes.follow_profile_image(b, "desktop")     # needs 1280: destroy
    assert b.image[0] == "main"


async def test_list_rows_flag_a_pending_profile_image(on, monkeypatch):
    from backend import vm_api
    b = boxes.allocate("project", project="a", variant="main")
    b.ctl = _Ctl(True)

    async def prof(slug):
        return {"separate_box": 1, "box_image": "e2epip"}
    monkeypatch.setattr(boxes, "project_profile", prof)
    row = await vm_api._box_row(b)
    assert row["image_pending"] == "e2epip" and row["restart_needed"] is True
