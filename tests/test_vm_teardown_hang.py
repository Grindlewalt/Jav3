"""e2e BUG-4: shutdown hung in vm.teardown() -> _kill_orphans, awaiting the exit
of a pkill child the loop never reaped. The helper subprocesses on the teardown
path now run in a thread with a timeout, never an awaited asyncio child."""
import asyncio
import subprocess

from backend.vm import lifecycle


def _no_asyncio_children(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("teardown must not await an asyncio child process")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)


async def test_kill_orphans_does_not_await_an_asyncio_child(monkeypatch, tmp_path):
    _no_asyncio_children(monkeypatch)
    calls = []
    monkeypatch.setattr(lifecycle.subprocess, "run",
                        lambda argv, **k: calls.append((argv, k.get("timeout"))))
    await asyncio.wait_for(lifecycle.vm._kill_orphans(), 5)
    assert calls and calls[0][0][:3] == ["pkill", "-9", "-f"] and calls[0][1]


async def test_kill_orphans_survives_a_hung_pkill(monkeypatch):
    def hung(argv, **k):
        raise subprocess.TimeoutExpired(argv, k.get("timeout"))
    monkeypatch.setattr(lifecycle.subprocess, "run", hung)
    await asyncio.wait_for(lifecycle.vm._kill_orphans(), 5)


async def test_net_down_does_not_await_an_asyncio_child(monkeypatch):
    _no_asyncio_children(monkeypatch)
    seen = []

    class R:
        returncode = 0
        stderr = b""
    monkeypatch.setattr(lifecycle.subprocess, "run",
                        lambda argv, **k: seen.append(argv) or R())
    await asyncio.wait_for(lifecycle.vm._net("down"), 5)
    assert seen and seen[0][-1] == "down"
