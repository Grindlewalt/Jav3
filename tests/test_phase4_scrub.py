"""Phase 4 (M4c): the single-guest idle scrub. Offline — boot/teardown are stubbed,
so this drives just the refcount + reaper policy: a turn pins the guest, the reaper
only reboots once the guest is idle past the window, and never while a turn holds it.
A warm pool would be the wrong fit for this Pi's memory, so isolation across operation
batches comes from rebooting the one guest while it's idle instead."""
import backend.vm.lifecycle as lc
from backend.vm.lifecycle import GuestVM


class _FakeProc:
    returncode = None                       # a live process


def _stub_boot_teardown(monkeypatch, vm, calls):
    async def fake_boot():
        vm._proc = _FakeProc()              # "running"
        vm._booted_at = 1000.0
        vm._idle_since = 1000.0
        calls.append("boot")

    async def fake_teardown():
        vm._proc = None
        calls.append("teardown")

    async def fake_ensure_locked():
        if not vm.running():
            await fake_boot()
    monkeypatch.setattr(vm, "boot", fake_boot)
    monkeypatch.setattr(vm, "teardown", fake_teardown)
    monkeypatch.setattr(vm, "_ensure_ready_locked", fake_ensure_locked)


async def test_acquire_release_refcount(monkeypatch):
    vm = GuestVM()
    _stub_boot_teardown(monkeypatch, vm, [])
    await vm.acquire()
    await vm.acquire()
    assert vm._inflight == 2 and vm.running()
    vm.release()
    assert vm._inflight == 1                     # still held by the other turn
    vm.release()
    assert vm._inflight == 0 and vm._idle_since is not None   # idle clock starts


async def test_reaper_noop_when_scrub_disabled(monkeypatch):
    monkeypatch.setattr(lc.settings, "vm_idle_scrub_seconds", 0)
    vm = GuestVM()
    calls = []
    _stub_boot_teardown(monkeypatch, vm, calls)
    await vm.acquire()
    vm.release()
    monkeypatch.setattr(lc.time, "monotonic", lambda: 9999.0)   # long past idle
    await vm.reap_if_idle()
    assert calls == ["boot"]        # never rebooted — scrubbing is off


async def test_reaper_reboots_only_when_idle_past_window(monkeypatch):
    monkeypatch.setattr(lc.settings, "vm_idle_scrub_seconds", 60)
    vm = GuestVM()
    calls = []
    _stub_boot_teardown(monkeypatch, vm, calls)
    await vm.acquire()
    # a turn holds it — no scrub even if the clock is way ahead (inflight > 0)
    monkeypatch.setattr(lc.time, "monotonic", lambda: 100000.0)
    await vm.reap_if_idle()
    assert calls == ["boot"]
    # release at t=1000 -> the idle clock starts there
    monkeypatch.setattr(lc.time, "monotonic", lambda: 1000.0)
    vm.release()
    # +30s: still within the 60s window -> no reboot
    monkeypatch.setattr(lc.time, "monotonic", lambda: 1030.0)
    await vm.reap_if_idle()
    assert calls == ["boot"]
    # +100s: idle past the window -> reboot (teardown + boot), fresh next batch
    monkeypatch.setattr(lc.time, "monotonic", lambda: 1100.0)
    await vm.reap_if_idle()
    assert calls == ["boot", "teardown", "boot"]
