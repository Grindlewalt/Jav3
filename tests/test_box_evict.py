"""e2e (minor): an idle project box holding the only project slot blocked
another project's turn with "project box cap reached (1/1)". It now gives way;
a box with a turn bound or in flight never does."""
import pytest

from backend.config import settings
from backend.vm import boxes


@pytest.fixture
def on(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    monkeypatch.setattr(settings, "vm_max_project_boxes", 1)
    monkeypatch.setattr(settings, "vm_guest_ram_budget_mb", 10**6)
    boxes.registry.reset()

    async def prof(slug):
        return {"separate_box": 1, "box_image": "main", "box_runtime": "kvm"}
    monkeypatch.setattr(boxes, "project_profile", prof)
    destroyed = []

    async def destroy(box, delete_data=False):
        destroyed.append(box.id)
        boxes.registry.release(box.id)
    monkeypatch.setattr(boxes, "destroy", destroy)
    yield destroyed
    boxes.registry.reset()


async def test_idle_box_gives_way(on):
    a = await boxes.for_project("alpha")
    b = await boxes.for_project("beta")
    assert on == [a.id] and b.id == "p-beta"


async def test_busy_box_is_never_evicted(on):
    a = await boxes.for_project("alpha")
    boxes.registry.bind_op("op-1", a)
    with pytest.raises(boxes.BoxCapError):
        await boxes.for_project("beta")
    assert on == []


async def test_a_no_project_chat_store_runs_on_the_shared_box(on):
    """A project-less chat's guest workspace is its artifact store (chat-<id>):
    that must not allocate a project box per scratch chat (WEBA-01)."""
    box = await boxes.for_project("chat-41")
    assert box.id == boxes.SHARED_ID and on == []
